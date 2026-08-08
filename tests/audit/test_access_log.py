"""The Marco Civil access log: written for every request, purged at six months.

The ordering assertion is the one that matters. Registered inside the tenant
transaction, this middleware would have its row rolled back whenever the request
failed — which is to say, the access record would be missing for exactly the requests
an intrusion investigation starts from, and present for all the boring ones.

Bearer credentials are kept OUT of the recorded path. Invitation, email-confirmation
and password-reset links all outlive their usefulness long before this table's
six-month retention ends. `AccessLogMiddleware` therefore derives credential-bearing
requests from the shared route-name registry and replaces only the credential. The
reset user's `uidb36` survives, as does allauth's non-secret `set-password` marker, so
the keyed 302 and the password POST remain distinct forensic records.

SCOPE BOUNDARY, stated here so nobody later mistakes this for full-stack credential
hygiene. The redaction asserted below covers THIS application's own `audit_accesslog`
rows and claims nothing whatsoever about anything else. A fronting proxy, a load
balancer, a CDN, a web-server access log or an observability agent that records request
URLs still sees the raw token in transit, and so does the browser history of whoever
opened the link. Closing those surfaces is infrastructure work, deliberately outside
this change, and the tests below must not be read as evidence that it was done.

For a valid reset, allauth stores the key in the session and redirects to the
`set-password` URL before rendering the form. The reset exposure asserted here is the
one initial keyed GET, not a same-origin Referer chain that allauth already prevents.
"""

import re
from datetime import timedelta
from http import HTTPStatus
from typing import Final

import pytest
from django.conf import settings
from django.core import mail
from django.test import Client
from django.urls import resolve, reverse
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.audit.models import AccessLog
from apps.audit.tasks import purge_access_logs
from apps.clients.models import ClientCompany
from apps.core.rls import is_exempt_from_tenant_policy
from apps.core.tenancy import tenant_context
from apps.tenants.models import Invite, Membership, Tenant, TenantRole
from tests.support import enrol_totp

pytestmark = pytest.mark.django_db(transaction=True)

TENANT_HOST = "alpha.localhost"

# The two hosts that answer an acceptance link, and they are genuinely different doors.
# The firm-side route is mounted at the PLATFORM level (`apps/accounts/views.py` says
# why a subdomain cannot serve it), while the portal route lives on the portal urlconf
# and is reached through `HostDispatchMiddleware`. They share one path shape, which is
# exactly why one prefix covers both.
FIRM_ROUTE: Final = "firm"
PORTAL_ROUTE: Final = "portal"
FIRM_URLCONF: Final = "config.urls"
PORTAL_URLCONF: Final = "apps.portal.urls"
PLATFORM_HOST: Final = "testserver"
PORTAL_HOST: Final = "alpha-portal.localhost"

INVITE_PATH_PREFIX: Final = "/convites/aceitar/"
REDACTED_INVITE_PATH: Final = f"{INVITE_PATH_PREFIX}<redacted>/"
INVITED: Final = "convidada@alpha.example"
PADARIA_CNPJ: Final = "11222333000181"
QUERY_STRING: Final = "?origem=email"
CONFIRM_EMAIL_PATH_PREFIX: Final = "/accounts/confirm-email/"
RESET_KEY_PATH_PREFIX: Final = "/accounts/password/reset/key/"
RESET_LINK_PATH: Final = re.compile(r"/accounts/password/reset/key/[\w-]+/")


@pytest.fixture(autouse=True)
def _urls(settings: SettingsWrapper) -> None:
    settings.ROOT_URLCONF = "tests.audit.urls"
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]


@pytest.fixture
def tenant() -> Tenant:
    return Tenant.objects.create(name="Alpha", slug="alpha")


@pytest.fixture
def member(tenant: Tenant) -> User:
    user = User.objects.create_user(email="owner@alpha.example")
    Membership.objects.create(user=user, tenant=tenant, role=TenantRole.OWNER)
    enrol_totp(user)
    return user


def test_an_authenticated_request_is_logged_with_its_tenant(
    client: Client,
    tenant: Tenant,
    member: User,
) -> None:
    # Given a member on their firm's subdomain
    client.force_login(member)

    # When they make a request
    client.get("/audit-ok/", headers={"host": TENANT_HOST})

    # Then exactly one record carries the resolved tenant and the acting user
    row = AccessLog.objects.get(path="/audit-ok/")
    assert row.tenant_id == tenant.pk
    assert row.user_id == member.pk
    assert row.method == "GET"
    assert row.status_code == HTTPStatus.OK


def test_an_anonymous_request_is_logged_with_a_null_tenant(client: Client) -> None:
    # Given no session
    # When a request is made against the platform host
    client.get("/ping/")

    # Then it is still recorded — a fail-closed policy would have refused this insert,
    # losing exactly the pre-authentication records an investigation begins with
    row = AccessLog.objects.get(path="/ping/")
    assert row.tenant_id is None
    assert row.user_id is None


def test_a_failed_request_is_still_logged(
    client: Client,
    tenant: Tenant,
    member: User,
) -> None:
    # Given a member
    client.force_login(member)

    # When the request is refused after the view raised
    response = client.get("/audit-deny/", headers={"host": TENANT_HOST})
    assert response.status_code == HTTPStatus.FORBIDDEN

    # Then the access record survives the rollback that erased the view's own writes.
    # This is the whole reason the middleware sits outside the tenant transaction.
    row = AccessLog.objects.get(path="/audit-deny/")
    assert row.status_code == HTTPStatus.FORBIDDEN
    assert row.tenant_id == tenant.pk


def test_a_request_refused_by_tenant_resolution_is_still_logged(
    client: Client,
    tenant: Tenant,
) -> None:
    # Given an authenticated user with no membership for this firm
    outsider = User.objects.create_user(email="fora@outra.example")
    other = Tenant.objects.create(name="Beta", slug="beta")
    Membership.objects.create(user=outsider, tenant=other, role=TenantRole.OWNER)
    enrol_totp(outsider)
    client.force_login(outsider)

    # When they probe another firm's subdomain and TenantMiddleware refuses them
    response = client.get("/audit-ok/", headers={"host": TENANT_HOST})
    assert response.status_code == HTTPStatus.FORBIDDEN

    # Then the probe is on record. TenantMiddleware raises BEFORE it calls the rest of
    # the chain, so an access log registered inside it would never run at all — every
    # membership-denied request, which is the shape tenant probing takes, would go
    # entirely unlogged.
    row = AccessLog.objects.get(path="/audit-ok/")
    assert row.status_code == HTTPStatus.FORBIDDEN
    assert row.user_id == outsider.pk
    assert row.tenant_id is None
    assert tenant.slug == "alpha"


def test_the_access_log_middleware_runs_outside_the_tenant_transaction() -> None:
    # Given the shipped middleware stack
    middleware = list(settings.MIDDLEWARE)

    # When the two positions are compared
    access = middleware.index("apps.audit.middleware.AccessLogMiddleware")
    tenant_mw = middleware.index("apps.tenants.middleware.TenantMiddleware")

    # Then the access log is outer, so its write is never inside the rolled-back
    # transaction. core.E007 fails startup if this is ever reversed.
    assert access < tenant_mw


def test_the_access_log_table_is_allow_listed() -> None:
    # Given the row-level-security allow-list
    # When the access-log table is classified
    # Then it is exempt: a fail-closed policy would refuse anonymous requests
    assert is_exempt_from_tenant_policy("audit_accesslog")


def _seed_aged(days: int, path: str) -> AccessLog:
    row = AccessLog.objects.create(method="GET", path=path, status_code=200)
    AccessLog.objects.filter(pk=row.pk).update(
        created_at=timezone.now() - timedelta(days=days),
    )
    return row


def test_the_purge_deletes_past_retention_and_keeps_within_it() -> None:
    # Given one record either side of the six-month boundary
    old = _seed_aged(181, "/velho/")
    recent = _seed_aged(179, "/recente/")

    # When the retention sweep runs
    deleted = purge_access_logs()

    # Then only the one past retention is gone
    assert deleted == 1
    assert not AccessLog.objects.filter(pk=old.pk).exists()
    assert AccessLog.objects.filter(pk=recent.pk).exists()


def test_the_purge_task_is_on_the_beat_schedule() -> None:
    # Given the Celery beat schedule
    schedule = settings.CELERY_BEAT_SCHEDULE

    # When the retention sweep is looked for
    # Then it is scheduled. A purge nobody runs is a retention policy in name only.
    assert any(
        entry["task"] == "apps.audit.tasks.purge_access_logs"
        for entry in schedule.values()
    )
    assert settings.ACCESS_LOG_RETENTION_DAYS == 180


@pytest.fixture
def _shipped_urls(_urls: None, settings: SettingsWrapper) -> None:
    """Serve the real URL tree, because the redaction is about real routes.

    Declared as a plain fixture rather than autouse so it composes with `_urls` above
    instead of fighting it: the module's other tests need the stub urlconf, and the ones
    below need `config.urls`, which is the only tree that carries `invite-accept`. The
    portal route needs no such help — `HostDispatchMiddleware` points the request at
    `apps.portal.urls` off the Host header alone, whatever `ROOT_URLCONF` says.
    """
    settings.ROOT_URLCONF = FIRM_URLCONF


def _open_invitation_link(
    tenant: Tenant,
    route: str,
    query: str = "",
) -> tuple[AccessLog, str]:
    """Mint a real invitation, open its link on the matching host, return the record.

    Every non-vacuity gate lives HERE rather than in the callers, and that placement is
    deliberate: each caller below asserts that something is ABSENT from the recorded
    path, and an absence assertion is satisfied by an empty needle, by a request that
    never reached the route, and — most quietly of all — by a middleware that simply
    stopped writing rows. Gates in the callers could be deselected one at a time with
    `pytest -k`; gates in the shared helper cannot be reached around at all.

    So, in order: the token is a real non-empty string that is genuinely IN the URL and
    is not what the database stored; the request actually rendered the acceptance page
    rather than 404ing past the view; and exactly one access record exists, still
    carrying the prefix that names the route.
    """
    if route == PORTAL_ROUTE:
        with tenant_context(tenant.id):
            # ALL_OBJECTS_OK: fixture seeding under an established tenant context.
            company = ClientCompany.all_objects.create(
                tenant=tenant,
                legal_name="PADARIA ALPHA",
                cnpj=PADARIA_CNPJ,
                is_mei=True,
            )
        invite, raw_token = Invite.issue(
            tenant=tenant,
            email=INVITED,
            role=TenantRole.CLIENT_OWNER,
            client=company,
        )
        path = reverse("portal-invite-accept", args=[raw_token], urlconf=PORTAL_URLCONF)
        host = PORTAL_HOST
    else:
        invite, raw_token = Invite.issue(
            tenant=tenant,
            email=INVITED,
            role=TenantRole.STAFF_ACCOUNTANT,
        )
        path = reverse("invite-accept", args=[raw_token], urlconf=FIRM_URLCONF)
        host = PLATFORM_HOST
    path = f"{path}{query}"

    # Gate one: there is a needle. An empty token, or one that turned out to be the
    # stored digest, would make every "the token is absent" assertion below pass while
    # scanning for nothing.
    assert raw_token, "no raw token was minted, so the absence scan has no needle"
    assert invite.token != raw_token, "the raw token was stored instead of its digest"
    assert raw_token in path, "the token is not in the URL, so nothing could leak"

    response = Client().get(path, headers={"host": host})

    # Gate two: this was the acceptance page. A 404 from a route that never resolved
    # would also carry no token by the end, for entirely the wrong reason.
    assert response.status_code == HTTPStatus.OK, (
        f"{host}{path} answered {response.status_code}; the redaction assertions below "
        f"are only meaningful once the real acceptance view has served the link"
    )

    # Gate three: a row was WRITTEN, and it still names the route. Redaction is not
    # exemption — the visit remains on record, which is the whole Marco Civil duty, and
    # a middleware that quietly stopped recording would satisfy every other assertion
    # in this file's redaction tests.
    rows = list(AccessLog.objects.filter(path__startswith=INVITE_PATH_PREFIX))
    assert len(rows) == 1, (
        f"expected exactly one access record under {INVITE_PATH_PREFIX}, found "
        f"{[row.path for row in rows]}; redaction must not stop the visit being logged"
    )
    return rows[0], raw_token


@pytest.mark.parametrize("route", [FIRM_ROUTE, PORTAL_ROUTE])
@pytest.mark.usefixtures("_shipped_urls")
def test_an_invitation_token_never_reaches_the_access_record(
    tenant: Tenant,
    route: str,
) -> None:
    # Given a real invitation, opened at the door it belongs to
    # When the link is followed
    row, raw_token = _open_invitation_link(tenant, route)

    # Then the route is still legible and the bearer credential is gone. Both URL trees
    # resolve the same registered route shape, so the route-name gate must cover both.
    assert row.path == REDACTED_INVITE_PATH
    assert raw_token not in row.path


@pytest.mark.parametrize("route", [FIRM_ROUTE, PORTAL_ROUTE])
@pytest.mark.usefixtures("_shipped_urls")
def test_a_query_string_survives_the_redaction_untouched(
    tenant: Tenant,
    route: str,
) -> None:
    # Given an acceptance link opened with a query string attached
    # When the visit is recorded
    row, raw_token = _open_invitation_link(tenant, route, query=QUERY_STRING)

    # Then only the path segment holding the credential was rewritten. `get_full_path()`
    # is path AND query, so a redaction written against the whole string would either
    # swallow the query or miss the token depending on which end it cut from — and the
    # query is the half that carries the campaign, the referrer and the diagnostics an
    # access log is read for.
    assert row.path == f"{REDACTED_INVITE_PATH}{QUERY_STRING}"
    assert raw_token not in row.path


@pytest.mark.parametrize("path", ["/", "/clientes/"])
@pytest.mark.usefixtures("_shipped_urls")
def test_a_path_carrying_no_credential_is_recorded_byte_for_byte(path: str) -> None:
    # Given an ordinary request holding no credential in its path
    Client().get(path, headers={"host": PLATFORM_HOST})

    # Then it is recorded exactly as requested. The registry-backed route-name gate
    # must not rewrite ordinary paths and silently damage the incident record.
    assert [row.path for row in AccessLog.objects.all()] == [path]


@pytest.mark.parametrize(
    "tree",
    [(FIRM_URLCONF, PLATFORM_HOST), (PORTAL_URLCONF, PORTAL_HOST)],
)
@pytest.mark.parametrize(
    "credential",
    [
        (
            "account_confirm_email",
            {"key": "confirmacao-viva-7c93f"},
            CONFIRM_EMAIL_PATH_PREFIX,
            "confirmacao-viva-7c93f",
            f"{CONFIRM_EMAIL_PATH_PREFIX}<redacted>/",
        ),
        (
            "account_reset_password_from_key",
            {"uidb36": "2s", "key": "reset-vivo-7c93f"},
            RESET_KEY_PATH_PREFIX,
            "reset-vivo-7c93f",
            f"{RESET_KEY_PATH_PREFIX}2s-<redacted>/",
        ),
    ],
)
@pytest.mark.usefixtures("_shipped_urls")
def test_allauth_credentials_never_reach_access_records(
    tenant: Tenant,
    tree: tuple[str, str],
    credential: tuple[str, dict[str, str], str, str, str],
) -> None:
    urlconf, host = tree
    url_name, kwargs, prefix, raw_key, expected_path = credential
    assert tenant.slug == "alpha"
    path = reverse(url_name, kwargs=kwargs, urlconf=urlconf)
    assert path.startswith(prefix)

    response = Client().get(path, headers={"host": host})
    assert response.status_code != HTTPStatus.NOT_FOUND
    rows = list(AccessLog.objects.filter(path__startswith=prefix))
    assert len(rows) == 1
    row = rows[0]
    print(  # noqa: T201 - the red-state artifact must expose the stored leak
        f"LEAKED audit_accesslog.path host={host} route={url_name} "
        f"raw_key={raw_key} stored_path={row.path}",
    )
    assert row.path == expected_path
    assert raw_key not in row.path


@pytest.mark.parametrize(
    "tree",
    [(FIRM_URLCONF, PLATFORM_HOST), (PORTAL_URLCONF, PORTAL_HOST)],
)
@pytest.mark.parametrize(
    "control",
    [
        ("account_email_verification_sent", CONFIRM_EMAIL_PATH_PREFIX),
        (
            "account_reset_password_from_key_done",
            f"{RESET_KEY_PATH_PREFIX}done/",
        ),
    ],
)
@pytest.mark.usefixtures("_shipped_urls")
def test_non_secret_allauth_siblings_are_recorded_unchanged(
    tenant: Tenant,
    tree: tuple[str, str],
    control: tuple[str, str],
) -> None:
    urlconf, host = tree
    url_name, expected_path = control
    assert tenant.slug == "alpha"
    path = reverse(url_name, urlconf=urlconf)
    assert path == expected_path

    response = Client().get(path, headers={"host": host})
    assert response.status_code != HTTPStatus.NOT_FOUND
    row = AccessLog.objects.get()
    assert row.path == path


@pytest.mark.usefixtures("_shipped_urls")
def test_a_live_reset_keeps_the_uid_and_distinguishes_get_from_post(
    member: User,
) -> None:
    client = Client()
    requested = client.post(
        reverse("account_reset_password"),
        {"email": member.email},
        headers={"host": PLATFORM_HOST},
    )
    assert requested.status_code == HTTPStatus.FOUND
    assert mail.outbox
    found = RESET_LINK_PATH.search(str(mail.outbox[-1].body))
    assert found is not None
    raw_path = found.group(0)
    assert raw_path.startswith(RESET_KEY_PATH_PREFIX)
    match = resolve(raw_path, urlconf=FIRM_URLCONF)
    assert match.url_name == "account_reset_password_from_key"
    uidb36 = str(match.kwargs["uidb36"])
    raw_key = str(match.kwargs["key"])
    assert raw_key

    clicked = client.get(raw_path, headers={"host": PLATFORM_HOST})
    assert clicked.status_code == HTTPStatus.FOUND
    set_password_path = str(clicked.headers["Location"])
    assert set_password_path == reverse(
        "account_reset_password_from_key",
        kwargs={"uidb36": uidb36, "key": "set-password"},
        urlconf=FIRM_URLCONF,
    )
    submitted = client.post(
        set_password_path,
        {
            "password1": "Nova-senha-segura-7c93f",
            "password2": "Nova-senha-segura-7c93f",
        },
        headers={"host": PLATFORM_HOST},
    )
    assert submitted.status_code == HTTPStatus.FOUND

    rows = list(
        AccessLog.objects.filter(path__startswith=RESET_KEY_PATH_PREFIX).order_by(
            "created_at",
        ),
    )
    assert len(rows) == 2
    clicked_row, submitted_row = rows
    assert clicked_row.method == "GET"
    assert clicked_row.status_code == HTTPStatus.FOUND
    assert clicked_row.path == f"{RESET_KEY_PATH_PREFIX}{uidb36}-<redacted>/"
    assert raw_key not in clicked_row.path
    assert submitted_row.method == "POST"
    assert submitted_row.path == set_password_path
    assert submitted_row.path != clicked_row.path
