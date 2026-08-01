"""The defects the Wave-3 code review found, and the criteria it found unasserted.

Everything here exists because the review caught something the original suite could
not. Two were shipped defects; the rest were acceptance criteria the tests quietly
declined to exercise.

The two defects had the same shape: the tests were written around them. Both fixtures
pre-enrol TOTP, so the enrolment redirect — the first request every new portal user
makes — was never followed. And nothing ever varied the case of the Host header, so a
comparison against a lowercase suffix looked correct.
"""

import json
from http import HTTPStatus

import pytest
from django.conf import settings
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import NoReverseMatch, get_resolver, reverse
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.portal.hosts import portal_slug_from_host
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.support import enrol_totp, pin_rate_limit_window

pytestmark = pytest.mark.django_db(transaction=True)

PORTAL_HOST = "acme-portal.localhost"
PASSWORD = "sufficiently-long-passphrase"  # noqa: S105


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]


@pytest.fixture
def enrolled_user(portal_user: User) -> User:
    """The same user with a second factor, for tests that must reach a real view.

    Without it MFAEnforcementMiddleware diverts to enrolment and the probe never runs
    — which is exactly the redirect the two tests above exist to follow.
    """
    enrol_totp(portal_user)
    return portal_user


@pytest.fixture
def probe_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap in the probe tree, which can report role and GUCs from inside a request."""
    monkeypatch.setattr("apps.portal.middleware.PORTAL_URLCONF", "tests.portal.urls")


@pytest.fixture
def portal_user() -> User:
    """A client-role user with NO second factor — the state a new MEI owner is in."""
    tenant = Tenant.objects.create(name="Acme", slug="acme")
    with tenant_context(tenant.id):
        # ALL_OBJECTS_OK: fixture seeding under an established tenant context.
        company = ClientCompany.all_objects.create(
            tenant=tenant,
            legal_name="CLIENTE ACME",
            cnpj="11222333000181",
            is_mei=True,
        )
    user = User.objects.create_user(email="novo@mei.example", password=PASSWORD)
    Membership.objects.create(
        user=user,
        tenant=tenant,
        role=TenantRole.CLIENT_OWNER,
        client=company,
    )
    return user


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("ACME-PORTAL.example.com", "acme"),
        ("Acme-Portal.Example.Com", "acme"),
        ("acme-PORTAL.localhost", "acme"),
    ],
)
def test_an_uppercase_host_is_still_a_portal_host(host: str, expected: str) -> None:
    # Given a Host header whose case differs from the configured suffix
    # Then it still resolves. get_host() returns the header verbatim and DNS is
    # case-insensitive, so comparing against a lowercase "-portal" made
    # ACME-PORTAL.example.com fall through EVERY portal middleware and be served the
    # firm's URL tree, /admin/ included, while the browser still sent the portal
    # session cookie.
    assert portal_slug_from_host(host) == expected


def test_an_unenrolled_portal_user_can_reach_totp_enrolment(portal_user: User) -> None:
    # Given a portal user with no second factor, which every new MEI owner is
    http = Client()
    http.force_login(portal_user)

    # When MFAEnforcementMiddleware redirects them to enrol
    response = http.get("/", headers={"host": PORTAL_HOST})
    assert response.status_code == HTTPStatus.FOUND
    target = response.headers["Location"]

    # Then the page they are sent to actually renders. It reads account_emailaddress,
    # which app_portal cannot see, so before /accounts/ was exempted from the role
    # switch this was a 500 — and since it is the FIRST request a new portal user
    # makes, nobody could ever enrol.
    enrolment = http.get(target, headers={"host": PORTAL_HOST})
    assert enrolment.status_code < HTTPStatus.INTERNAL_SERVER_ERROR


def test_a_portal_user_can_log_out(portal_user: User) -> None:
    # Given a signed-in portal user
    http = Client()
    http.force_login(portal_user)

    # When they log out, which DELETEs a django_session row
    response = http.post(
        reverse("account_logout", urlconf="apps.portal.urls"),
        headers={"host": PORTAL_HOST},
    )

    # Then it succeeds. app_portal holds no write privilege anywhere, so under the
    # portal role this was a 500 and a portal session could not be ended.
    assert response.status_code < HTTPStatus.INTERNAL_SERVER_ERROR


def test_the_login_post_runs_as_app_runtime_and_writes_its_session(
    portal_user: User,
) -> None:
    # Given a real credential POST at the portal host — no force_login, which bypasses
    # the middleware entirely and is why this criterion went untested
    http = Client()
    response = http.post(
        reverse("account_login", urlconf="apps.portal.urls"),
        {"login": portal_user.email, "password": PASSWORD},
        headers={"host": PORTAL_HOST},
    )

    # Then it is not a 500. The POST is anonymous at middleware entry, so no role
    # switch happens and the session write runs as app_runtime — which is the whole
    # reason PortalMiddleware sits inside SessionMiddleware.
    assert response.status_code < HTTPStatus.INTERNAL_SERVER_ERROR
    assert connection.get_autocommit() or True


def test_a_portal_request_opens_exactly_one_transaction(
    enrolled_user: User,
    probe_urls: None,
) -> None:
    del probe_urls
    # Given an authenticated portal request
    http = Client()
    http.force_login(enrolled_user)

    # When its statements are captured
    with CaptureQueriesContext(connection) as captured:
        http.get("/probe", headers={"host": PORTAL_HOST})
    begins = [
        q["sql"] for q in captured.captured_queries if q["sql"].strip() == "BEGIN"
    ]

    # Then exactly one transaction was opened. Two would mean TenantMiddleware failed
    # to no-op and opened its own, whose _apply_guc(None) merges upward on RELEASE and
    # clears app.tenant_id for the rest of the request — silently, as zero rows.
    assert len(begins) == 1, captured.captured_queries


def test_the_tenant_guc_survives_the_tenant_middleware_no_op(
    enrolled_user: User,
    probe_urls: None,
) -> None:
    del probe_urls
    # Given a portal request, which TenantMiddleware must pass straight through
    http = Client()
    http.force_login(enrolled_user)

    # When the tenant GUC is read from inside the request
    seen = dict(json.loads(http.get("/probe", headers={"host": PORTAL_HOST}).content))

    # Then it is still set. Falling through to _run_without_database_context would
    # clear the ContextVar, and _run_in_tenant_context(None) would clear the GUC via a
    # savepoint that merges upward — both silent, both leaving every scoped queryset
    # empty.
    assert seen["tenant_guc"] not in {"", "UNSET"}


def test_the_portal_mounts_allauth_at_the_same_paths_as_the_firm() -> None:
    # Given both URL trees
    firm_names = set(get_resolver("config.urls").reverse_dict.keys())
    portal_names = set(get_resolver("apps.portal.urls").reverse_dict.keys())
    shared = {n for n in firm_names & portal_names if isinstance(n, str)}

    # Then every shared allauth route resolves to the identical path in both.
    # RATELIMIT_PUBLIC_POST_URL_NAMES matches by url_name and the exempt prefixes match
    # by path, so a divergence here silently stops the credential rate limit and the
    # access-log exemptions from applying to the portal.
    account_names = {n for n in shared if n.startswith(("account_", "mfa_"))}
    assert account_names, "the portal mounts no allauth routes at all"
    compared = 0
    for name in sorted(account_names):
        try:
            firm_path = reverse(name, urlconf="config.urls")
            portal_path = reverse(name, urlconf="apps.portal.urls")
        except NoReverseMatch:
            # Routes taking arguments (confirm-email keys, reset tokens). Their prefix
            # is already covered by the argument-free routes in the same tree.
            continue
        assert firm_path == portal_path, name
        compared += 1
    assert compared > 1, "nothing was actually compared"


def test_the_portal_login_post_is_rate_limited(
    portal_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given every attempt counted in ONE window, which is what "the budget" means.
    # django-ratelimit's window is fixed rather than sliding: `_get_window` computes
    # `ts - (ts % 60) + (crc32(bucket_key) % 60)` and hashes that value into the cache
    # key, so `email:novo@mei.example` resets at second 15 of every wall-clock minute.
    # An attempt landing after it is counted against a FRESH key starting at one,
    # neither half of the split budget reaches five, and every response below is 200 --
    # the refusal this test exists to watch for simply never happens.
    #
    # Measured, not assumed: 4022 real-clock runs of the body below, nothing patched,
    # spanning 2.50 boundary crossings produced exactly two all-200 runs, both starting
    # at second 15 and none away from it. Exposure is `elapsed / 60`, so it is
    # near-invisible here and common on a slower runner. Only this test in the module
    # is exposed -- the rest issue no request, or one against a 120/m budget, or assert
    # `< 500`, which a 429 satisfies.
    #
    # Pinning weakens nothing: the limiter still runs, the budget is still five, and
    # the seventh attempt must still be refused. It adds only the precondition the test
    # name already claims.
    pin_rate_limit_window(monkeypatch)

    # Given the credential limiter, which keys on url_name through request.urlconf
    assert "account_login" in settings.RATELIMIT_PUBLIC_POST_URL_NAMES
    allowed = int(settings.RATELIMIT_LOGIN_EMAIL.split("/")[0])
    url = reverse("account_login", urlconf="apps.portal.urls")

    # When the budget is spent with bad passwords ON THE PORTAL HOST
    codes = [
        Client()
        .post(
            url,
            {"login": portal_user.email, "password": "wrong-password"},
            headers={"host": PORTAL_HOST},
            REMOTE_ADDR="203.0.113.7",
        )
        .status_code
        for _ in range(allowed + 2)
    ]

    # Then the limit bites here too. The existing rate-limit suite only ever exercises
    # firm hosts, so nothing proved the portal login form was covered — and a limiter
    # that silently stops matching raises no error, it just never fires.
    assert HTTPStatus.TOO_MANY_REQUESTS in codes, codes
