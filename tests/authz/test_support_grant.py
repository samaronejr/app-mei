"""Owner-approved, time-limited support grants — every denial proved over HTTP.

The matrix asserts the architecture, not just the decoration: a support session
is admitted ONLY when a live `SupportGrant` row exists, the row is re-read from
the database on every request (never cached, never a session flag), and the
capabilities a session inherits are the owner's decision — read surfaces FULL,
writes NONE, `billing.manage` only under `read_write_settings`.

`transaction=True` throughout: the denials below exercise `SET LOCAL` and the
RLS layer, which are meaningless inside a surrounding test transaction.
"""

import re
from datetime import timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from allauth.account.models import EmailAddress
from allauth.account.views import logout
from django.contrib.auth import SESSION_KEY
from django.contrib.sessions.models import Session
from django.db import IntegrityError, connection, transaction
from django.http import Http404
from django.test import Client
from django.urls import include, path, reverse
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.audit.models import AuditAction, Event, PlatformEvent
from apps.authz.support import SupportContext, current_support
from apps.clients.models import ClientContact
from apps.core.tenancy import current_tenant_id, tenant_context
from apps.delivery.models import MessageState, OutboundMessage
from apps.delivery.notice_authority import (
    NoticeOrigin,
    bind_notice,
    configure,
    lock_notice_scope,
)
from apps.delivery.policy import content_digest
from apps.tenants.models import Membership, Tenant, TenantRole
from apps.tenants.services import lock_firm_settings
from apps.tenants.support import (
    SupportApproval,
    approve_support_grant,
    request_support_grant,
    revoke_support_grant,
)
from apps.tenants.support_models import SupportGrant, SupportScope
from tests.browser.harness import mfa_session_cookie
from tests.cycles.approval_fixtures import approval_graph
from tests.cycles.cycle_fixtures import CycleSetup, cycle_setup
from tests.cycles.test_retention_dry_run import old_original
from tests.isolation.rolecheck import assert_isolated_role
from tests.support import enrol_totp, pinned_totp_window, totp_code
from tests.tenants import urls as tenant_test_urls
from tests.tenants.test_cancellation_retention import enabled_firm
from tests.ui.factories import PASSWORD, Firm, add_member

if TYPE_CHECKING:
    from django.urls import URLPattern, URLResolver

pytestmark = pytest.mark.django_db(transaction=True)

STAFF_EMAIL = "support@platform.example.com"
PURPOSE = "Cliente não consegue baixar o DAS"


def _staff(email: str = STAFF_EMAIL) -> User:
    """A platform account: staff, never a member of any firm."""
    return User.objects.create_user(email=email, password=PASSWORD, is_staff=True)


def _session_for(user: User, host: str) -> Client:
    """Sign `user` in by session cookie with a real enrolled second factor."""
    from allauth.mfa.models import Authenticator  # noqa: PLC0415

    if not Authenticator.objects.filter(
        user=user, type=Authenticator.Type.TOTP
    ).exists():
        enrol_totp(user)
    session = Client(SERVER_NAME=host)
    cookie = mfa_session_cookie(user, f"http://{host}/")
    session.cookies[cookie["name"]] = cookie["value"]
    return session


def _grant_for(  # noqa: PLR0913 — one decision per kwarg
    tenant: Tenant,
    owner: User,
    staff: User,
    *,
    scope: str = SupportScope.READ_ONLY,
    client_ids: tuple[UUID, ...] = (),
    hours: int = 4,
) -> SupportGrant:
    """Create a live grant through the real request + approval services."""
    with tenant_context(tenant.pk):
        grant = request_support_grant(staff, tenant.pk, purpose=PURPOSE)
        approve_support_grant(
            owner,
            tenant.pk,
            grant.pk,
            SupportApproval(scope=scope, client_ids=client_ids, hours=hours),
        )
        grant.refresh_from_db()
    return grant


def _pending(tenant: Tenant, staff: User) -> SupportGrant:
    """A requested-but-undecided grant."""
    with tenant_context(tenant.pk):
        return request_support_grant(staff, tenant.pk, purpose=PURPOSE)


def _firm_host(firm: Firm) -> str:
    return f"{firm.tenant.slug}.localhost"


def _contact_for(setup: CycleSetup) -> ClientContact:
    """A real recipient row; OutboundMessage's contact FK is NOT NULL."""
    with tenant_context(setup.tenant.pk):
        return ClientContact.objects.create(
            tenant=setup.tenant,
            client=setup.client,
            name="Contato",
            email="contato@client.example",
        )


def _enabled_setup(slug: str) -> CycleSetup:
    """cycle_setup with the settings feature T22's /configuracoes/ routes need."""
    setup = cycle_setup(slug)
    setup.tenant.features = [*setup.tenant.features, "firm_settings_r1"]
    setup.tenant.save(update_fields=["features"])
    return setup


# ---------------------------------------------------------------- admission


def test_no_grant_denies_the_firm_host() -> None:
    """A platform account with no seat and no grant is refused with a 403."""
    firm = enabled_firm("support-no-grant")
    staff = _staff()
    session = _session_for(staff, _firm_host(firm))
    assert session.get("/").status_code == HTTPStatus.FORBIDDEN


def test_pending_grant_denies_until_the_owner_decides() -> None:
    """The request alone admits nothing — approval is the gate."""
    firm = enabled_firm("support-pending")
    staff = _staff()
    _pending(firm.tenant, staff)
    session = _session_for(staff, _firm_host(firm))
    assert session.get("/").status_code == HTTPStatus.FORBIDDEN


def test_expired_mid_session_denies_the_next_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expiry is re-read per request: past expires_at the session dies at once."""
    firm = enabled_firm("support-expired")
    staff = _staff()
    grant = _grant_for(firm.tenant, firm.owner, staff, hours=1)
    session = _session_for(staff, _firm_host(firm))
    assert session.get("/").status_code == HTTPStatus.OK

    expires_at = grant.expires_at
    assert expires_at is not None
    # The approved window is immutable. Cross its exact boundary with the clock,
    # not by rewriting the owner's decision or waiting for wall time.
    monkeypatch.setattr("apps.authz.support.timezone.now", lambda: expires_at)

    assert session.get("/").status_code == HTTPStatus.FORBIDDEN


def test_revoke_denies_the_next_request() -> None:
    """The owner's revoke POST kills the session before the next page."""
    firm = enabled_firm("support-revoke")
    staff = _staff()
    grant = _grant_for(firm.tenant, firm.owner, staff)
    session = _session_for(staff, _firm_host(firm))
    assert session.get("/").status_code == HTTPStatus.OK

    owner = _session_for(firm.owner, _firm_host(firm))
    assert (
        owner.post(f"/configuracoes/suporte/{grant.pk}/revogar/").status_code
        == HTTPStatus.FOUND
    )
    assert session.get("/").status_code == HTTPStatus.FORBIDDEN


def test_support_session_is_recorded_once_and_carries_the_banner() -> None:
    """First page under a grant records support.session_started and the banner."""
    firm = enabled_firm("support-session")
    staff = _staff()
    _grant_for(firm.tenant, firm.owner, staff)
    session = _session_for(staff, _firm_host(firm))

    first = session.get("/")
    assert first.status_code == HTTPStatus.OK
    assert b"data-support-banner" in first.content
    assert "Acesso de suporte ativo" in first.content.decode()

    assert session.get("/").status_code == HTTPStatus.OK

    events = PlatformEvent.objects.filter(action=AuditAction.SUPPORT_SESSION_STARTED)
    assert events.count() == 1
    assert events.get().actor == staff


def test_owner_page_lists_and_approves_a_request() -> None:
    """The owner sees the pending request; approving it opens the support door."""
    firm = enabled_firm("support-owner-page")
    staff = _staff()
    grant = _pending(firm.tenant, staff)

    owner = _session_for(firm.owner, _firm_host(firm))
    page = owner.get("/configuracoes/suporte/")
    assert page.status_code == HTTPStatus.OK
    html = page.content.decode()
    assert STAFF_EMAIL in html
    assert PURPOSE in html

    response = owner.post(
        f"/configuracoes/suporte/{grant.pk}/aprovar/",
        {"scope": SupportScope.READ_ONLY, "hours": 2},
    )
    assert response.status_code == HTTPStatus.FOUND

    with tenant_context(firm.tenant.pk):
        grant.refresh_from_db()
    assert grant.is_active()
    assert grant.approved_by_owner == firm.owner
    assert grant.expires_at is not None
    assert grant.starts_at is not None
    assert grant.expires_at - grant.starts_at == timedelta(hours=2)


def test_each_pending_request_names_its_own_client_choices() -> None:
    """Two pending requests: every id is unique and each label ticks its own form."""
    firm = enabled_firm("support-two-requests")
    first = _pending(firm.tenant, _staff())
    second = _pending(firm.tenant, _staff(email="support-second@platform.example.com"))

    owner = _session_for(firm.owner, _firm_host(firm))
    html = owner.get("/configuracoes/suporte/").content.decode()

    ids = re.findall(r'\bid="([^"]+)"', html)
    assert len(ids) == len(set(ids)), sorted({i for i in ids if ids.count(i) > 1})
    for grant in (first, second):
        article = html.split(f'data-grant="{grant.pk}"', 1)[1].split("</article>", 1)[0]
        boxes = re.findall(
            r'<input type="checkbox" name="client_ids"[^>]*\bid="([^"]+)"', article
        )
        assert len(boxes) == len(firm.clients), article
        assert all(f'for="{box}"' in article for box in boxes), article


def _active_grant(firm: Firm, staff: User) -> SupportGrant:
    grant = _grant_for(firm.tenant, firm.owner, staff)
    session = _session_for(staff, _firm_host(firm))
    session.get("/")
    return grant


def test_owner_sees_the_support_audit_trail() -> None:
    """B5: the grant's own page lists every request made under it.

    Owner-only, RLS-scoped, and newest first — requests, plus the lifecycle
    transitions the grant has already crossed.
    """
    firm = enabled_firm("support-trail-owner")
    staff = _staff()
    grant = _active_grant(firm, staff)
    session = _session_for(firm.owner, _firm_host(firm))

    page = session.get(f"/configuracoes/suporte/{grant.pk}/trilha/")
    assert page.status_code == HTTPStatus.OK
    html = page.content.decode()
    assert staff.email in html
    # The request row itself — method and path on the grant's own page.
    assert "Requisição" in html
    assert "GET" in html
    # And the lifecycle that granted it.
    assert "Acesso autorizado" in html


def test_the_support_trail_stays_owner_only() -> None:
    """B5: a non-owner member, a foreign firm and a support session all miss it."""
    firm = enabled_firm("support-trail-scope")
    other = enabled_firm("support-trail-other")
    staff = _staff()
    grant = _active_grant(firm, staff)

    member = add_member(
        firm.tenant, "member-trail@firm.example.com", TenantRole.STAFF_ACCOUNTANT
    )
    member_session = _session_for(member, _firm_host(firm))
    other_session = _session_for(other.owner, _firm_host(other))
    staff_session = _session_for(staff, _firm_host(firm))

    for s in (member_session, other_session, staff_session):
        r = s.get(f"/configuracoes/suporte/{grant.pk}/trilha/")
        assert r.status_code in (HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND)


def test_only_the_owner_can_decide() -> None:
    """A firm member who is not the owner cannot approve a grant."""
    firm = enabled_firm("support-member-cannot")
    staff = _staff()
    grant = _pending(firm.tenant, staff)
    member = add_member(
        firm.tenant, "staff-member@firm.example.com", TenantRole.STAFF_ACCOUNTANT
    )
    member_session = _session_for(member, _firm_host(firm))

    response = member_session.post(
        f"/configuracoes/suporte/{grant.pk}/aprovar/",
        {"scope": SupportScope.READ_ONLY, "hours": 2},
    )
    assert response.status_code in (HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND)
    with tenant_context(firm.tenant.pk):
        grant.refresh_from_db()
    assert grant.approved_by_owner is None


def test_grant_is_per_person_not_per_tenant() -> None:
    """A different staff account on the same firm gets nothing from another's grant."""
    firm = enabled_firm("support-per-person")
    _grant_for(firm.tenant, firm.owner, _staff())
    other = _staff(email="support-other@platform.example.com")
    session = _session_for(other, _firm_host(firm))
    assert session.get("/").status_code == HTTPStatus.FORBIDDEN


# ---------------------------------------------------------------- writes


def test_approvals_stay_denied_under_a_grant() -> None:
    """actions.approve resolves NONE: a support session cannot approve."""
    scenario = approval_graph("support-approve-denied")
    graph = scenario.graph
    graph.tenant.features = [*graph.tenant.features, "firm_settings_r1"]
    graph.tenant.save(update_fields=["features"])
    staff = _staff()
    _grant_for(
        graph.tenant,
        graph.actor,
        staff,
        client_ids=(graph.client.pk,),
    )

    session = _session_for(staff, f"{graph.tenant.slug}.localhost")
    card = session.get(f"/acoes/{scenario.proposal.pk}/")
    assert card.status_code == HTTPStatus.OK
    # A support visit carries no addressee away: contact names stay unresolved.
    assert scenario.contact.name not in card.content.decode()

    response = session.post(
        f"/acoes/{scenario.proposal.pk}/",
        {"action": "approve", "expected_version": scenario.proposal.version},
    )
    assert response.status_code == HTTPStatus.FORBIDDEN


def test_document_download_denied_under_a_grant() -> None:
    """documents.transfer resolves NONE: support never receives document bytes."""
    setup = _enabled_setup("support-download")
    with tenant_context(setup.tenant.pk):
        document = old_original(setup)
    staff = _staff()
    _grant_for(setup.tenant, setup.actor, staff, client_ids=(setup.client.pk,))

    session = _session_for(staff, f"{setup.tenant.slug}.localhost")
    response = session.get(f"/documentos/{document.pk}/baixar/")
    assert response.status_code == HTTPStatus.FORBIDDEN


def test_preview_capability_is_denied_in_support_mode() -> None:
    """The capability the preview sits behind resolves NONE inside a session."""
    setup = _enabled_setup("support-preview")
    staff = _staff()
    _grant_for(setup.tenant, setup.actor, staff, client_ids=(setup.client.pk,))

    from apps.authz.services import can as authz_can  # noqa: PLC0415
    from apps.authz.support import (  # noqa: PLC0415
        SupportContext,
        current_support,
    )

    with tenant_context(setup.tenant.pk):
        grant = SupportGrant.objects.get(requested_by=staff)
        token = current_support.set(
            SupportContext(
                grant_id=grant.pk,
                tenant_id=setup.tenant.pk,
                scope=grant.scope,
                client_ids=frozenset(grant.client_uuid_set()),
                expires_at=grant.expires_at,
            )
        )
        try:
            assert (
                authz_can(staff, "documents.transfer", tenant_id=setup.tenant.pk)
                is False
            )
            assert (
                authz_can(staff, "clients.view_assigned", tenant_id=setup.tenant.pk)
                is True
            )
        finally:
            current_support.reset(token)


def test_message_subject_and_note_are_redacted_in_support_mode() -> None:
    """The queue proves the timeline happened; subject and note are replaced."""
    setup = _enabled_setup("support-redact")
    staff = _staff()
    _grant_for(setup.tenant, setup.actor, staff, client_ids=(setup.client.pk,))

    contact = _contact_for(setup)
    with tenant_context(setup.tenant.pk):
        configure(setup.actor, kinds=("notice",), acknowledged=True, expected_version=0)
        scope = lock_notice_scope(setup.actor, NoticeOrigin("notice", setup.client.pk))
        message = OutboundMessage(
            tenant=setup.tenant,
            client=setup.client,
            contact=contact,
            created_by=setup.actor,
            kind="notice",
            idempotency_key="support-redact-1",
            business_intent_key="support-redact-1",
            recipients_json=[contact.email],
            subject="PRIVATE SUBJECT CANARY",
            body_ref="digest-body-ref",
            portal_url="https://example.test/x",
            state=MessageState.QUEUED,
            decision_note="PRIVATE NOTE CANARY",
        )
        message.content_digest = content_digest(message)
        bind_notice(scope, message)
        message.save(force_insert=True)

    session = _session_for(staff, f"{setup.tenant.slug}.localhost")
    page = session.get("/comunicacao/")
    assert page.status_code == HTTPStatus.OK
    html = page.content.decode()
    assert "PRIVATE SUBJECT CANARY" not in html
    assert "PRIVATE NOTE CANARY" not in html
    assert "•••" in html


def test_send_decision_post_is_denied_under_a_grant() -> None:
    """cycles.operate resolves NONE: a support session cannot decide a send."""
    setup = _enabled_setup("support-send-denied")
    staff = _staff()
    _grant_for(setup.tenant, setup.actor, staff, client_ids=(setup.client.pk,))

    contact = _contact_for(setup)
    with tenant_context(setup.tenant.pk):
        configure(setup.actor, kinds=("notice",), acknowledged=True, expected_version=0)
        scope = lock_notice_scope(setup.actor, NoticeOrigin("notice", setup.client.pk))
        message = OutboundMessage(
            tenant=setup.tenant,
            client=setup.client,
            contact=contact,
            created_by=setup.actor,
            kind="notice",
            idempotency_key="support-send-1",
            business_intent_key="support-send-1",
            recipients_json=[contact.email],
            subject="queued",
            body_ref="ref",
            portal_url="https://example.test/x",
            state=MessageState.UNKNOWN,
        )
        message.content_digest = content_digest(message)
        bind_notice(scope, message)
        message.save(force_insert=True)

    session = _session_for(staff, f"{setup.tenant.slug}.localhost")
    response = session.post(
        "/comunicacao/",
        {
            "message_id": str(message.pk),
            "expected_version": message.version,
            "action": "sent",
            "note": "marcar como enviado",
        },
    )
    assert response.status_code in (HTTPStatus.FORBIDDEN, HTTPStatus.CONFLICT, 422)


def test_client_ids_scope_intersects_visible_clients() -> None:
    """Empty client_ids shows firm-level only; listed ids are the whole book."""
    firm = enabled_firm("support-scope")
    staff = _staff()
    _grant_for(firm.tenant, firm.owner, staff, client_ids=(firm.clients[0].pk,))
    empty_staff = _staff(email="support-empty@platform.example.com")
    _grant_for(firm.tenant, firm.owner, empty_staff, client_ids=())

    host = _firm_host(firm)
    listed_page = _session_for(staff, host).get("/clientes/")
    assert listed_page.status_code == HTTPStatus.OK
    assert firm.clients[0].legal_name in listed_page.content.decode()

    empty_session = _session_for(empty_staff, host)
    assert empty_session.get("/").status_code == HTTPStatus.OK
    empty_page = empty_session.get("/clientes/")
    if empty_page.status_code == HTTPStatus.OK:
        assert firm.clients[0].legal_name not in empty_page.content.decode()


def test_superuser_still_needs_a_grant_for_tenant_data() -> None:
    """Platform tables stay open; the firm's pages refuse without a grant."""
    firm = enabled_firm("support-superuser")
    admin = User.objects.create_superuser(
        email="super@platform.example.com", password=PASSWORD
    )
    session = _session_for(admin, _firm_host(firm))
    assert session.get("/").status_code == HTTPStatus.FORBIDDEN
    platform = _session_for(admin, "localhost")
    assert platform.get("/admin/").status_code == HTTPStatus.OK


def test_db_enforces_the_72_hour_window() -> None:
    """The CHECK refuses a longer window no matter who writes the row."""
    firm = enabled_firm("support-72h")
    staff = _staff()
    with tenant_context(firm.tenant.pk):
        grant = request_support_grant(staff, firm.tenant.pk, purpose=PURPOSE)
        grant.starts_at = timezone.now()
        grant.expires_at = timezone.now() + timedelta(hours=73)
        grant.approved_by_owner = firm.owner
        grant.approved_at = timezone.now()
        # Nested atomic so the CHECK's rollback lands on a savepoint and the
        # tenant_context block's exit write still runs against a live tx.
        with pytest.raises(IntegrityError), transaction.atomic():
            grant.save()


def test_support_grant_rows_are_rls_isolated() -> None:
    """Raw SQL from another tenant's context sees nothing of this firm's grants."""
    assert_isolated_role()
    firm = enabled_firm("support-rls")
    other = enabled_firm("support-rls-other")
    staff = _staff()
    _pending(firm.tenant, staff)

    with tenant_context(other.tenant.pk), connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM tenants_supportgrant WHERE tenant_id = %s",
            [str(firm.tenant.pk)],
        )
        assert cursor.fetchone()[0] == 0

    with tenant_context(firm.tenant.pk), connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM tenants_supportgrant WHERE tenant_id = %s",
            [str(firm.tenant.pk)],
        )
        assert cursor.fetchone()[0] == 1


def test_automations_console_is_denied_in_support_mode() -> None:
    """AXR1-027's Automações surface: delivery.automations resolves NONE.

    The placed-stack page (`/gestao/automacoes/`) exposes firm pause controls and
    dead-letter retry/cancel POSTs. support_mode answers NONE for
    `delivery.automations`, so a granted session cannot even read the console —
    and pause/retry/cancel can never fire from it.
    """
    firm = enabled_firm("support-automacoes")
    staff = _staff()
    _grant_for(firm.tenant, firm.owner, staff)

    session = _session_for(staff, _firm_host(firm))
    assert session.get("/gestao/automacoes/").status_code == HTTPStatus.FORBIDDEN
    assert (
        session.post(
            "/gestao/automacoes/",
            {"action": "pause", "expected_version": "1"},
        ).status_code
        == HTTPStatus.FORBIDDEN
    )


def test_owner_audit_trail_covers_request_approval_and_revoke() -> None:
    """The firm's own Event log carries every transition, not just the platform."""
    firm = enabled_firm("support-audit")
    staff = _staff()
    grant = _grant_for(firm.tenant, firm.owner, staff)

    with tenant_context(firm.tenant.pk):
        revoke_support_grant(firm.owner, firm.tenant.pk, grant.pk)
        actions = set(
            Event.objects.filter(
                action__in=(
                    AuditAction.SUPPORT_GRANT_REQUESTED,
                    AuditAction.SUPPORT_GRANT_APPROVED,
                    AuditAction.SUPPORT_GRANT_REVOKED,
                )
            ).values_list("action", flat=True)
        )
    assert AuditAction.SUPPORT_GRANT_REQUESTED in actions
    assert AuditAction.SUPPORT_GRANT_APPROVED in actions
    assert AuditAction.SUPPORT_GRANT_REVOKED in actions

    assert PlatformEvent.objects.filter(
        action=AuditAction.SUPPORT_GRANT_REQUESTED
    ).exists()


# ----------------------------------------------------------- review blockers


def test_empty_client_ids_admin_shows_no_client_rows() -> None:
    """B1: an empty-scope grant is firm-metadata-only, admin included.

    The admin console selects the firm through the session and binds the same
    grant as current_support; ClientCompanyAdmin must answer the empty queryset
    the firm-host portfolio already answers, not the firm's whole book.
    """
    firm = enabled_firm("support-admin-empty")
    staff = _staff(email="support-admin@platform.example.com")
    staff.is_superuser = True
    staff.save(update_fields=["is_superuser"])
    _grant_for(firm.tenant, firm.owner, staff, client_ids=())

    session = _session_for(staff, "localhost")
    # Select the firm in the admin console (the session key the middleware reads).
    sel = session.post(
        "/admin/selecionar-empresa/",
        {"tenant": str(firm.tenant.pk), "reason": "AXR1-023 B1 repro"},
    )
    assert sel.status_code in (HTTPStatus.FOUND, HTTPStatus.OK)
    changelist = session.get("/admin/clients/clientcompany/")
    assert changelist.status_code == HTTPStatus.OK
    assert firm.clients[0].legal_name not in changelist.content.decode()
    # The change form for a row the scope excludes never renders: the admin
    # answers a redirect to the index or a hard refusal, not the client's page.
    row = session.get(f"/admin/clients/clientcompany/{firm.clients[0].pk}/change/")
    assert row.status_code in (
        HTTPStatus.FORBIDDEN,
        HTTPStatus.NOT_FOUND,
        HTTPStatus.FOUND,
    )


def test_every_request_under_a_grant_writes_dual_audit() -> None:
    """B2: each support request lands in BOTH audit streams, GETs included."""
    firm = enabled_firm("support-audit-req")
    staff = _staff()
    _grant_for(firm.tenant, firm.owner, staff)
    session = _session_for(staff, _firm_host(firm))

    assert session.get("/").status_code == HTTPStatus.OK
    assert session.get("/clientes/").status_code == HTTPStatus.OK

    platform = PlatformEvent.objects.filter(action=AuditAction.SUPPORT_REQUEST)
    assert platform.count() == 2
    with tenant_context(firm.tenant.pk):
        tenant_events = Event.objects.filter(action=AuditAction.SUPPORT_REQUEST)
        assert tenant_events.count() == 2


def test_revoked_grant_cannot_write_mid_request() -> None:
    """B3: a write re-checks the grant row itself, not the request stash.

    The stash is bound once per request; a revocation landing between bind and
    write must still fail closed. The stale-stash write proves it.
    """
    firm = enabled_firm("support-revoke-write")
    staff = _staff()
    grant = _grant_for(
        firm.tenant,
        firm.owner,
        staff,
        scope=SupportScope.READ_WRITE_SETTINGS,
    )
    session = _session_for(staff, _firm_host(firm))
    assert session.get("/").status_code == HTTPStatus.OK

    # The owner's revoke takes effect on the very next request, mid-session.
    with tenant_context(firm.tenant.pk):
        revoke_support_grant(firm.owner, firm.tenant.pk, grant.pk)

    assert session.get("/").status_code == HTTPStatus.FORBIDDEN

    # And a service call handed the stale stash must still fail closed: the
    # write path re-reads the grant row, never trusts the bound snapshot.
    with tenant_context(firm.tenant.pk):
        stale = SupportContext(
            grant_id=grant.pk,
            tenant_id=firm.tenant.pk,
            scope=SupportScope.READ_WRITE_SETTINGS,
            client_ids=frozenset(),
            expires_at=timezone.now() + timedelta(hours=1),
        )
        binding = current_support.set(stale)
        try:
            # lock_firm_settings refuses the same way it refuses a non-member:
            # no visible tenant row, so the answer is a 404, never the write.
            with pytest.raises(Http404):
                lock_firm_settings(staff, firm.tenant.pk)
        finally:
            current_support.reset(binding)


# ----------------------------------------------------------- logout recovery


def _csrf_from(content: bytes) -> str:
    token = re.search(rb'name="csrfmiddlewaretoken" value="([^"]+)"', content)
    assert token is not None
    return token.group(1).decode()


def _login_with_csrf(session: Client, user: User, *, step: int = 0) -> None:
    """Use password and real TOTP on the same host-bound, CSRF-enforcing client."""
    EmailAddress.objects.get_or_create(
        user=user,
        email=user.email,
        defaults={"verified": True, "primary": True},
    )
    page = session.get(reverse("account_login"))
    assert page.status_code == HTTPStatus.OK
    password = session.post(
        reverse("account_login"),
        {
            "login": user.email,
            "password": PASSWORD,
            "csrfmiddlewaretoken": _csrf_from(page.content),
        },
    )
    assert password.status_code == HTTPStatus.FOUND
    assert password["Location"] == reverse("mfa_authenticate")
    assert SESSION_KEY not in session.session
    challenge = session.get(reverse("mfa_authenticate"))
    assert challenge.status_code == HTTPStatus.OK
    with pinned_totp_window(step=step):
        second_factor = session.post(
            reverse("mfa_authenticate"),
            {"code": totp_code(), "csrfmiddlewaretoken": _csrf_from(challenge.content)},
        )
    assert second_factor.status_code == HTTPStatus.FOUND
    assert session.session[SESSION_KEY] == str(user.pk)


def _denied_login(
    firm: Firm, denial: str, foreign_path: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[Client, User]:
    """Start a real session and cross the specified admission boundary."""
    grant = None
    if denial == "membership-lost":
        actor = firm.accountant
    elif denial == "portal-seat":
        actor = add_member(
            firm.tenant,
            "logout-portal@client.example",
            TenantRole.CLIENT_OWNER,
            client=firm.clients[0],
        )
    else:
        actor = _staff()
        enrol_totp(actor)
        grant = _grant_for(
            firm.tenant, firm.owner, actor, client_ids=(firm.clients[0].pk,)
        )
    session = Client(SERVER_NAME=firm.host, enforce_csrf_checks=True)
    _login_with_csrf(session, actor)
    if denial != "portal-seat":
        assert session.get("/").status_code == HTTPStatus.OK
        assert session.get(foreign_path).status_code == HTTPStatus.NOT_FOUND
    if denial.startswith("revoked"):
        assert grant is not None
        with tenant_context(firm.tenant.pk):
            revoke_support_grant(firm.owner, firm.tenant.pk, grant.pk)
    elif denial == "expired":
        assert grant is not None
        boundary = grant.expires_at
        assert boundary is not None
        monkeypatch.setattr("apps.authz.support.timezone.now", lambda: boundary)
    elif denial == "membership-lost":
        Membership.objects.filter(user=actor, tenant=firm.tenant).update(
            is_active=False
        )
    if denial == "revoked-fresh":
        session = Client(SERVER_NAME=firm.host, enforce_csrf_checks=True)
        _login_with_csrf(session, actor, step=1)
    return session, actor


@pytest.mark.parametrize(
    "denial",
    ["revoked-live", "revoked-fresh", "expired", "membership-lost", "portal-seat"],
)
def test_denied_account_can_logout_and_switch_with_csrf(
    denial: str, monkeypatch: pytest.MonkeyPatch, settings: SettingsWrapper
) -> None:
    # Given a real MFA session, denied by a live authorization change or client seat.
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]
    firm = enabled_firm(f"logout-{denial}")
    other = enabled_firm(f"logout-other-{denial}")
    foreign_path = f"/clientes/{other.clients[0].pk}/"
    session, actor = _denied_login(firm, denial, foreign_path, monkeypatch)
    assert session.cookies[settings.SESSION_COOKIE_NAME]["domain"] == ""
    old_key = session.session.session_key
    assert old_key is not None
    for protected in (
        "/",
        "/clientes/",
        f"/clientes/{firm.clients[0].pk}/",
        foreign_path,
        reverse("account_email"),
        reverse("account_change_password"),
        "/accounts/logout/not-registered/",
        "/clientes/?next=/accounts/logout/",
    ):
        denied = session.get(protected)
        assert denied.status_code == HTTPStatus.FORBIDDEN
        assert firm.clients[0].legal_name not in denied.content.decode()
        assert other.clients[0].legal_name not in denied.content.decode()

    # When the registered confirmation and then its CSRF-protected POST are used.
    logout_path = reverse("account_logout")
    confirmation = session.get(logout_path)
    assert confirmation.status_code == HTTPStatus.OK
    assert session.session[SESSION_KEY] == str(actor.pk)
    assert session.session.session_key == old_key
    assert getattr(confirmation.wsgi_request, "resolved_tenant", None) == firm.tenant
    assert hasattr(confirmation.wsgi_request, "tenant")
    assert getattr(confirmation.wsgi_request, "tenant", None) is None
    assert getattr(confirmation.wsgi_request, "support_grant", None) is None
    assert current_tenant_id.get() is None
    assert current_support.get() is None
    assert firm.clients[0].legal_name not in confirmation.content.decode()
    assert other.clients[0].legal_name not in confirmation.content.decode()
    csrf = _csrf_from(confirmation.content)
    assert session.post(logout_path).status_code == HTTPStatus.FORBIDDEN
    assert (
        session.post(
            logout_path,
            {"csrfmiddlewaretoken": csrf},
            HTTP_ORIGIN="https://attacker.invalid",
        ).status_code
        == HTTPStatus.FORBIDDEN
    )
    assert session.session[SESSION_KEY] == str(actor.pk)
    assert (
        session.post("/clientes/", {"csrfmiddlewaretoken": csrf}).status_code
        == HTTPStatus.FORBIDDEN
    )
    exited = session.post(
        logout_path,
        {"csrfmiddlewaretoken": csrf, "next": "https://attacker.invalid/"},
    )

    # Then real server-side logout occurs, with no unsafe redirect or data admission.
    assert exited.status_code == HTTPStatus.FOUND
    assert exited["Location"] == "/"
    assert not exited.wsgi_request.user.is_authenticated
    assert SESSION_KEY not in session.session
    assert not Session.objects.filter(session_key=old_key).exists()
    assert session.cookies[settings.SESSION_COOKIE_NAME]["domain"] == ""
    _login_with_csrf(session, firm.owner)
    assert session.cookies[settings.SESSION_COOKIE_NAME]["domain"] == ""
    assert session.get("/").status_code == HTTPStatus.OK
    assert session.get(foreign_path).status_code == HTTPStatus.NOT_FOUND
    for protected in ("/", f"/clientes/{other.clients[0].pk}/"):
        denied = session.get(protected, HTTP_HOST=other.host)
        assert denied.status_code == HTTPStatus.FORBIDDEN
        assert other.clients[0].legal_name not in denied.content.decode()


@pytest.mark.parametrize(
    ("name", "real_callback", "namespace"),
    [
        ("account_logout", False, ""),
        ("forged_logout", True, ""),
        ("account_logout", True, "forged"),
    ],
)
def test_logout_recovery_requires_registered_route_and_callback(
    name: str,
    real_callback: bool,
    namespace: str,
    monkeypatch: pytest.MonkeyPatch,
    settings: SettingsWrapper,
) -> None:
    # Given an authenticated account without a seat, and an impostor logout route.
    firm = enabled_firm("logout-impostor")
    staff = _staff()
    session = _session_for(staff, firm.host)
    routes: list[URLPattern | URLResolver] = [
        path(
            "accounts/logout/",
            logout if real_callback else tenant_test_urls.guc,
            name=name,
        )
    ]
    if namespace:
        routes = [path("", include((routes, namespace), namespace=namespace))]
    monkeypatch.setattr(tenant_test_urls, "urlpatterns", routes)
    settings.ROOT_URLCONF = "tests.tenants.urls"
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]

    # When the name alone, callback alone, or namespaced callback asks for entry.
    response = session.get("/accounts/logout/")

    # Then it is still denied and has not signed the account out.
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert session.session[SESSION_KEY] == str(staff.pk)


def test_active_support_logout_retains_its_granted_context() -> None:
    # Given an active grant, whose ordinary request context must still be audited.
    firm = enabled_firm("logout-active-support")
    staff = _staff()
    grant = _grant_for(firm.tenant, firm.owner, staff)
    session = _session_for(staff, firm.host)

    # When the normal logout route is reached and executed.
    confirmation = session.get(reverse("account_logout"))
    assert confirmation.status_code == HTTPStatus.OK
    assert getattr(confirmation.wsgi_request, "tenant", None) == firm.tenant
    assert getattr(confirmation.wsgi_request, "support_grant", None) == grant
    exited = session.post(reverse("account_logout"))

    # Then authorized support keeps its existing context; actor audit is T50's scope.
    assert exited.status_code == HTTPStatus.FOUND
    assert getattr(exited.wsgi_request, "tenant", None) == firm.tenant
    assert getattr(exited.wsgi_request, "support_grant", None) == grant
    assert SESSION_KEY not in session.session
