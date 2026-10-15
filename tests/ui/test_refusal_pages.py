"""A refusal page leaves the reader a way out (AXR1-059).

F3 met the shared 403 page three ways -- support access the owner revoked, a client's
seat used on the firm host, a client whose portal access ended -- and none of them
offered Sair or a way back: the account was signed in and stuck on a page that only
refused. It met the portfolio-first 404 as Django's bare English "Not Found", because
no `templates/404.html` existed.

Every case reads the HTTP response alone. Signing out is driven with the token the page
itself carries, through a client that enforces CSRF, so "a working Sair" means the form
on the refusal logs the account out. The 404 cases also hold the promise the page must
not undo: an address naming something outside the reader's book answers the same page,
byte for byte once the per-response CSRF token is set aside, as an address naming
nothing at all.

The review of that change found three more 403s a reader can reach outside this
layout, and the cases at the end of the module hold them to the same contract: the
CSRF refusal a stale tab meets (signing in rotates the CSRF secret, so a form drawn
before a sign-in in another tab no longer matches), a write while the firm is read
only, and the download of a file the upload scan refused. Each keeps its status, its
reason and, for the CSRF refusal, the refusal itself: the stale submission still
changes nothing.

The invitation refusals on both doors are held to it too. A signed-in account that
opens someone else's invitation is refused (403) and can leave, after which the same
browser redeems the link as the invited person; and a link that names nothing at a
door -- unknown, of the other kind of seat, or of another firm -- leads a visitor back
while staying one page, so it still says nothing about which of those it was.

Nothing here imports the templates under test, so the module collects on the parent
head too, where these assertions are expected to fail rather than the import.
"""

import html
import re
import uuid
from datetime import timedelta
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from allauth.account.models import EmailAddress
from django.conf import settings as django_settings
from django.contrib.auth import SESSION_KEY
from django.test import Client
from django.urls import reverse
from django.utils.timezone import localdate
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.invites import issue_client_invite, issue_invite
from apps.accounts.models import User
from apps.clients.maintenance import EngagementData, save_engagement
from apps.clients.views import ONBOARDING_FLAG
from apps.core.tenancy import tenant_context
from apps.cycles.evidence import REJECTED_STATES, DocumentIntake
from apps.obligations.models import Document
from apps.portal.middleware import ACCESS_ENDED
from apps.tenants.account_state import READ_ONLY_MESSAGE
from apps.tenants.models import BillingAccount, BillingStatus, Membership, TenantRole
from apps.tenants.support import (
    SupportApproval,
    approve_support_grant,
    request_support_grant,
    revoke_support_grant,
)
from apps.tenants.support_models import SupportScope
from tests.browser.harness import mfa_session_cookie
from tests.portal.request_fixtures import (
    OTHER_CLIENT_CANARY,
    PortalSetup,
    portal_setup,
    upload,
)
from tests.support import enrol_totp
from tests.ui.factories import PASSWORD, Firm, add_member, make_firm
from tests.ui.refusal_contract import H1, LANG, TAG, TITLE

if TYPE_CHECKING:  # stubs-only: this type does not exist at runtime
    from django.test.client import _MonkeyPatchedWSGIResponse

pytestmark = pytest.mark.django_db(transaction=True)

LOGOUT_PATH: Final = "/accounts/logout/"
FORM: Final = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.DOTALL | re.IGNORECASE)
SAIR: Final = re.compile(r"<button\b[^>]*type=\"submit\"[^>]*>\s*Sair\s*</button>")
CSRF_VALUE: Final = re.compile(r'(name="csrfmiddlewaretoken" value=)"[^"]+"')
WAY_BACK: Final = re.compile(r'<a\b[^>]*href="/"[^>]*>(.*?)</a>', re.DOTALL)
TOKEN: Final = re.compile(r'name="csrfmiddlewaretoken" value="([^"]+)"')
PORTAL_UPLOAD: Final = "/documentos/enviar"
NOT_A_PDF: Final = b"<!doctype html><script>alert(1)</script>"


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]


def _setup(slug: str) -> PortalSetup:
    """One R1 firm with settings on: two clients, a portal seat in the first."""
    setup = portal_setup(slug)
    tenant = setup.graph.tenant
    tenant.features = [*tenant.features, "firm_settings_r1"]
    tenant.save(update_fields=["features"])
    return setup


def _firm_host(setup: PortalSetup) -> str:
    return f"{setup.graph.tenant.slug}.localhost"


def _session(user: User, host: str) -> Client:
    """A signed-in session (password and TOTP) that enforces CSRF like a browser."""
    session = Client(SERVER_NAME=host, enforce_csrf_checks=True)
    cookie = mfa_session_cookie(user, f"http://{host}/")
    session.cookies[cookie["name"]] = cookie["value"]
    return session


def _text(fragment: str) -> str:
    return " ".join(html.unescape(TAG.sub(" ", fragment)).split())


def _document(response: "_MonkeyPatchedWSGIResponse", status: int) -> str:
    """Assert the status and a pt-BR document with a title, one heading, a way back."""
    body = response.content.decode()
    assert response.status_code == status, (response.status_code, body[:300])
    assert LANG in body, f"no pt-BR document language in {body[:300]!r}"
    title = TITLE.search(body)
    assert title is not None, f"no document title in {body[:300]!r}"
    assert _text(title.group(1)), "the document title is empty"
    headings = H1.findall(body)
    assert len(headings) == 1, f"expected one heading, found {headings!r}"
    assert _text(headings[0]), "the page heading is empty"
    back = WAY_BACK.search(body)
    assert back is not None, f"no way back to the start of this host in {body!r}"
    assert _text(back.group(1)), "the way back has no accessible name"
    assert "tabindex" not in back.group(0), "the way back is taken out of tab order"
    return body


def _sair_token(body: str) -> str | None:
    """Return the CSRF token of the page's POST Sair form, or None when it has none."""
    for attributes, inner in FORM.findall(body):
        if f'action="{LOGOUT_PATH}"' not in attributes:
            continue
        if 'method="post"' not in attributes.lower():
            continue
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', inner)
        if SAIR.search(inner) and token is not None:
            return token.group(1)
    return None


def _signs_out(session: Client, body: str) -> None:
    """Submit the page's own Sair form and prove the session really ended."""
    token = _sair_token(body)
    assert token is not None, f"the refusal offers no POST Sair to {LOGOUT_PATH}"
    exited = session.post(LOGOUT_PATH, {"csrfmiddlewaretoken": token})
    assert exited.status_code == HTTPStatus.FOUND, exited.status_code
    assert exited["Location"] == "/"
    assert SESSION_KEY not in session.session
    entrance = session.get("/")
    assert entrance.status_code == HTTPStatus.FOUND, entrance.status_code
    assert "/accounts/login/" in entrance["Location"]


def _support_revoked(setup: PortalSetup) -> tuple[Client, str]:
    """A platform operator whose grant the owner approved and then revoked."""
    staff = User.objects.create_user(
        email="suporte@plataforma.example.com", password=PASSWORD, is_staff=True
    )
    enrol_totp(staff)
    tenant = setup.graph.tenant
    with tenant_context(tenant.pk):
        grant = request_support_grant(staff, tenant.pk, purpose="Conferir o acesso")
        approve_support_grant(
            setup.graph.actor,
            tenant.pk,
            grant.pk,
            SupportApproval(
                scope=SupportScope.READ_ONLY,
                client_ids=(setup.graph.client.pk,),
                hours=4,
            ),
        )
        revoke_support_grant(setup.graph.actor, tenant.pk, grant.pk)
    return _session(staff, _firm_host(setup)), ""


def _client_on_firm_host(setup: PortalSetup) -> tuple[Client, str]:
    """The client's own portal account, used on the firm's host."""
    return _session(setup.portal_user, _firm_host(setup)), ""


def _portal_access_ended(setup: PortalSetup) -> tuple[Client, str]:
    """The client's portal seat after the firm recorded the engagement's end."""
    tenant = setup.graph.tenant
    with tenant_context(tenant.pk):
        engagement = setup.graph.engagement
        engagement.refresh_from_db()
        save_engagement(
            actor=setup.graph.actor,
            tenant_id=tenant.pk,
            client_id=setup.graph.client.pk,
            engagement_id=engagement.pk,
            expected_version=engagement.version,
            data=EngagementData(
                engagement.service_kind,
                engagement.starts_on,
                ends_on=localdate() - timedelta(days=1),
                responsible_id=setup.graph.actor.pk,
            ),
        )
    return _session(setup.portal_user, setup.portal_host), ACCESS_ENDED


REFUSALS: Final = {
    "support-revoked": _support_revoked,
    "client-on-firm-host": _client_on_firm_host,
    "portal-access-ended": _portal_access_ended,
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_a_refused_account_can_go_back_or_sign_out_from_the_403(
    case: str, settings: SettingsWrapper
) -> None:
    # Given a signed-in account the firm or the portal no longer admits
    settings.ENGAGEMENT_OFFBOARDING_GRACE_DAYS = 0
    setup = _setup(f"recusa-{case}")
    session, reason = REFUSALS[case](setup)

    # When it opens the start page of the host it is signed in on
    refused = session.get("/")

    # Then the refusal keeps its status and its reason, reveals no client, and leads out
    body = _document(refused, HTTPStatus.FORBIDDEN)
    assert reason in _text(body)
    assert setup.graph.client.legal_name not in body
    assert setup.other_client.legal_name not in body
    _signs_out(session, body)


def test_a_firm_404_is_one_page_whatever_the_address_names() -> None:
    # Given an accountant whose book holds no client, beside a client of the same firm
    setup = _setup("recusa-carteira")
    session = _session(setup.graph.staff, _firm_host(setup))

    # When they open that client, a client id naming nothing, and an unrouted address
    answers = [
        session.get(f"/clientes/{setup.graph.client.pk}/"),
        session.get(f"/clientes/{uuid.uuid4()}/"),
        session.get("/endereco-que-nao-existe/"),
    ]

    # Then each is the same pt-BR 404, which names neither the client nor the address
    bodies = [_document(answer, HTTPStatus.NOT_FOUND) for answer in answers]
    assert len({CSRF_VALUE.sub(r'\1""', body) for body in bodies}) == 1
    assert setup.graph.client.legal_name not in bodies[0]
    assert str(setup.graph.client.pk) not in bodies[0]
    assert "endereco-que-nao-existe" not in bodies[0]
    # And the account can leave from it
    _signs_out(session, bodies[0])


def test_a_portal_404_is_one_page_whatever_the_address_names() -> None:
    # Given a client's portal seat, beside another client's request in the same firm
    setup = _setup("recusa-portal-404")
    session = _session(setup.portal_user, setup.portal_host)

    # When it opens the other client's request and a request id naming nothing
    answers = [
        session.get(f"/solicitacoes/{setup.other_request.pk}/"),
        session.get(f"/solicitacoes/{uuid.uuid4()}/"),
    ]

    # Then both are the same pt-BR 404, and nothing of the other request shows
    bodies = [_document(answer, HTTPStatus.NOT_FOUND) for answer in answers]
    assert len({CSRF_VALUE.sub(r'\1""', body) for body in bodies}) == 1
    assert OTHER_CLIENT_CANARY not in bodies[0]
    assert str(setup.other_request.pk) not in bodies[0]
    # And the account can leave from it
    _signs_out(session, bodies[0])


def test_a_404_offers_a_visitor_who_is_not_signed_in_no_sair() -> None:
    # Given nobody signed in
    visitor = Client(SERVER_NAME="localhost")

    # When an address that names nothing is opened
    body = _document(visitor.get("/endereco-que-nao-existe/"), HTTPStatus.NOT_FOUND)

    # Then the page leads back to the start, and offers no session to end
    assert _sair_token(body) is None


def _page_token(session: Client, path: str = "/") -> str:
    """Return the CSRF token a page drawn now carries, as its tab would submit it."""
    page = session.get(path)
    assert page.status_code == HTTPStatus.OK, (path, page.status_code)
    token = TOKEN.search(page.content.decode())
    assert token is not None, f"{path} ({page.status_code}) carries no CSRF token"
    return token.group(1)


def _stale_token(session: Client, path: str = "/") -> str:
    """Return a token drawn before the browser's CSRF secret changed.

    A sign-in rotates the secret. Dropping the cookie and drawing the page again
    leaves the server the same state: the browser holds a new secret, and the
    older tab still holds a token minted from the previous one.
    """
    stale = _page_token(session, path)
    del session.cookies[django_settings.CSRF_COOKIE_NAME]
    _page_token(session, path)
    assert django_settings.CSRF_COOKIE_NAME in session.cookies, "no new CSRF secret"
    return stale


def _signed_in(setup: PortalSetup, host: str) -> Client:
    """The firm's owner on the firm host, or the client's seat on the portal host."""
    if host == "firm":
        return _session(setup.graph.actor, _firm_host(setup))
    return _session(setup.portal_user, setup.portal_host)


# (page the tab shows, address its form posts to)
STALE_TABS: Final = {
    # The shell's own Sair, sent from a tab drawn before the secret rotated.
    "firm": ("/", LOGOUT_PATH),
    # The portal's upload form, so the refusal is drawn under the portal role.
    "portal": ("/documentos/", PORTAL_UPLOAD),
}


@pytest.mark.parametrize("host", sorted(STALE_TABS))
def test_a_stale_tab_answers_an_expired_page_that_leads_out(host: str) -> None:
    # Given a signed-in account holding a tab drawn before its CSRF secret rotated
    setup = _setup(f"csrf-{host}")
    session = _signed_in(setup, host)
    page, action = STALE_TABS[host]
    stale = _stale_token(session, page)

    # When that tab submits its form
    refused = session.post(action, {"csrfmiddlewaretoken": stale})

    # Then CSRF still refuses it and the account is still signed in
    body = _document(refused, HTTPStatus.FORBIDDEN)
    assert SESSION_KEY in session.session
    # And the page says nothing of Django's developer reason, and leads out
    assert "CSRF" not in body, "Django's developer reason reached the reader"
    _signs_out(session, body)


def test_a_stale_sign_in_form_answers_the_expired_page_with_no_sair() -> None:
    # Given a visitor whose sign-in tab was drawn before the CSRF secret changed
    visitor = Client(SERVER_NAME="localhost", enforce_csrf_checks=True)
    stale = _stale_token(visitor, "/accounts/login/")

    # When that tab submits
    refused = visitor.post("/accounts/login/", {"csrfmiddlewaretoken": stale})

    # Then it is the expired page, with the way back and no session to end
    body = _document(refused, HTTPStatus.FORBIDDEN)
    assert _sair_token(body) is None


# (page the form is on, address it posts to)
READ_ONLY_WRITES: Final = {
    "firm": ("/clientes/novo/", "/clientes/novo/"),
    "portal": ("/documentos/", PORTAL_UPLOAD),
}


@pytest.mark.parametrize("host", sorted(READ_ONLY_WRITES))
def test_a_write_while_the_firm_is_read_only_answers_the_refusal_page(
    host: str,
) -> None:
    # Given a signed-in account with a form open when its firm was suspended
    setup = _setup(f"somente-leitura-{host}")
    tenant = setup.graph.tenant
    tenant.features = [*tenant.features, ONBOARDING_FLAG]
    tenant.save(update_fields=["features"])
    session = _signed_in(setup, host)
    page, action = READ_ONLY_WRITES[host]
    token = _page_token(session, page)
    with tenant_context(setup.graph.tenant.pk):
        BillingAccount.objects.update_or_create(
            tenant=setup.graph.tenant, defaults={"status": BillingStatus.SUSPENDED}
        )

    # When that form is submitted
    refused = session.post(action, {"csrfmiddlewaretoken": token})

    # Then the write is refused as before, by a page that says why and leads out
    body = _document(refused, HTTPStatus.FORBIDDEN)
    assert refused["X-Tenant-Read-Only"] == "1"
    assert READ_ONLY_MESSAGE in _text(body)
    _signs_out(session, body)


def test_a_download_the_scan_refused_answers_the_refusal_page(
    settings: SettingsWrapper, tmp_path: Path
) -> None:
    # Given a client upload the type check refused: kept, and never served
    settings.MEDIA_ROOT = tmp_path
    settings.CLAMAV_ENABLED = False
    setup = _setup("recusa-verificacao")
    vault = setup.portal().post(PORTAL_UPLOAD, {"file": upload("nota.pdf", NOT_A_PDF)})
    assert vault.status_code == HTTPStatus.BAD_REQUEST
    with tenant_context(setup.graph.tenant.pk):
        document = Document.objects.get(original_filename="nota.pdf")
        assert DocumentIntake.objects.get(document=document).state in REJECTED_STATES

    # When the firm's owner opens its download address
    session = _session(setup.graph.actor, _firm_host(setup))
    refused = session.get(reverse("staff-document-download", args=[document.pk]))

    # Then the download is refused as before, by a page that says why and leads out
    body = _document(refused, HTTPStatus.FORBIDDEN)
    assert "Arquivo recusado na verificação" in _text(body)
    _signs_out(session, body)


INVITED: Final = "convidada@padaria.example"
UNKNOWN_INVITE: Final = "nao-existe-este-convite"
REGISTRATION: Final = {
    "full_name": "Convidada",
    "password1": PASSWORD,
    "password2": PASSWORD,
}
DOORS: Final = ("firm", "portal")


def _invitation(door: str, firm: Firm) -> str:
    """Return the raw token of a live invitation to INVITED for that door's seat."""
    if door == "firm":
        _invite, raw = issue_invite(
            actor=firm.owner,
            tenant=firm.tenant,
            email=INVITED,
            role=TenantRole.STAFF_ACCOUNTANT,
        )
    else:
        _invite, raw = issue_client_invite(
            actor=firm.owner,
            tenant=firm.tenant,
            client=firm.clients[0],
            email=INVITED,
            role=TenantRole.CLIENT_OWNER,
        )
    return str(raw)


def _invite_host(door: str, firm: Firm) -> str:
    """Firm invitations are redeemed on the platform host, a client's on its portal."""
    return "localhost" if door == "firm" else f"{firm.tenant.slug}-portal.localhost"


def _invite_path(door: str, raw: str) -> str:
    if door == "firm":
        return f"/convites/aceitar/{raw}/"
    return str(reverse("portal-invite-accept", args=[raw], urlconf="apps.portal.urls"))


def _someone_else(door: str, firm: Firm) -> User:
    """A signed-in account whose address is not the one invited."""
    if door == "portal":
        return add_member(
            firm.tenant,
            "socia@padaria.example",
            TenantRole.CLIENT_OWNER,
            client=firm.clients[0],
        )
    other = User.objects.create_user(email="outra@qualquer.example", password=PASSWORD)
    EmailAddress.objects.create(
        user=other, email=other.email, verified=True, primary=True
    )
    enrol_totp(other)
    return other


@pytest.mark.parametrize("door", DOORS)
def test_an_invite_refused_to_another_account_leads_out_then_redeems(
    door: str,
) -> None:
    # Given an invitation, opened by a signed-in account with another address
    firm = make_firm(f"convite-{door}", client_count=1)
    path = _invite_path(door, _invitation(door, firm))
    session = _session(_someone_else(door, firm), _invite_host(door, firm))

    # When that account confirms the acceptance
    refused = session.post(path, {"csrfmiddlewaretoken": _page_token(session, path)})

    # Then it is refused as before, by a page with a way back and a working Sair
    body = _document(refused, HTTPStatus.FORBIDDEN)
    token = _sair_token(body)
    assert token is not None, f"the refusal offers no POST Sair to {LOGOUT_PATH}"
    exited = session.post(LOGOUT_PATH, {"csrfmiddlewaretoken": token})
    assert exited.status_code == HTTPStatus.FOUND, exited.status_code
    assert SESSION_KEY not in session.session
    # And, signed out, the same browser redeems the link as the person invited
    accepted = session.post(
        path, {**REGISTRATION, "csrfmiddlewaretoken": _page_token(session, path)}
    )
    assert accepted.status_code == HTTPStatus.FOUND, accepted.status_code
    with tenant_context(firm.tenant.pk):
        assert Membership.objects.filter(
            user__email=INVITED, tenant=firm.tenant
        ).exists()


@pytest.mark.parametrize("door", DOORS)
def test_an_invite_link_naming_nothing_here_leads_a_visitor_back(door: str) -> None:
    # Given links this door must not tell apart: one naming nothing, one for the
    # other kind of seat and, at the portal, one for another firm's portal
    firm = make_firm(f"porta-{door}", client_count=1)
    tokens = [UNKNOWN_INVITE, _invitation("portal" if door == "firm" else "firm", firm)]
    if door == "portal":
        tokens.append(_invitation("portal", make_firm("outra-porta", client_count=1)))
    visitor = Client(SERVER_NAME=_invite_host(door, firm))

    # When a visitor opens each
    answers = [visitor.get(_invite_path(door, raw)) for raw in tokens]

    # Then each is the same 404 page once the token it was asked about is set aside
    bodies = [
        _document(answer, HTTPStatus.NOT_FOUND).replace(raw, "<convite>")
        for answer, raw in zip(answers, tokens, strict=True)
    ]
    assert len(set(bodies)) == 1
    # And it leads back to the start, with no session to end
    assert _sair_token(bodies[0]) is None
