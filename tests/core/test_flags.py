"""Per-tenant feature flags: resolution, the kill switch, and the audit of changes.

`transaction=True` throughout, for the reason tests/portal/test_middleware.py
documents: under a plain `django_db` the portal middleware's `atomic()` degrades to a
savepoint and `SET LOCAL ROLE` merges upward, so every role-dependent assertion below
would pass for the wrong reason.

Every flag default is OFF: an R1 surface ships dark and turns on per cohort, so the
absence of a row value is a refusal, never a permission.
"""

from http import HTTPStatus

import pytest
from django.test import Client
from pytest_django.fixtures import DjangoAssertNumQueries, SettingsWrapper

from apps.accounts.models import User
from apps.audit.models import AuditAction, PlatformEvent
from apps.authz.stash import PortalStash, portal_stash
from apps.clients.models import ClientCompany
from apps.core.flags import (
    FEATURES,
    UnknownFeature,
    open_flag_cache,
    require_feature,
    tenant_has,
)
from apps.core.tenancy import tenant_context
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.support import enrol_totp

pytestmark = pytest.mark.django_db(transaction=True)

FIRM_HOST = "acme.localhost"
PORTAL_HOST = "acme-portal.localhost"
PASSWORD = "irrelevant-here"  # noqa: S105


@pytest.fixture(autouse=True)
def _urls(settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]
    settings.ROOT_URLCONF = "tests.core.urls"
    monkeypatch.setattr("apps.portal.middleware.PORTAL_URLCONF", "tests.portal.urls")


class Firm:
    """One firm, one of its clients, a firm-side user and a portal user."""

    def __init__(
        self,
        tenant: Tenant,
        client: ClientCompany,
        firm_user: User,
        portal_user: User,
    ) -> None:
        self.tenant = tenant
        self.client = client
        self.firm_user = firm_user
        self.portal_user = portal_user


@pytest.fixture
def firm() -> Firm:
    tenant = Tenant.objects.create(name="Acme", slug="acme")
    with tenant_context(tenant.id):
        # ALL_OBJECTS_OK: fixture seeding, before any request context exists.
        company = ClientCompany.all_objects.create(
            tenant=tenant,
            legal_name="CLIENTE ACME",
            cnpj="11222333000181",
            is_mei=True,
        )
    firm_user = User.objects.create_user(email="staff@acme.example", password=PASSWORD)
    portal_user = User.objects.create_user(email="mei@acme.example", password=PASSWORD)
    # Enrolled so MFAEnforcementMiddleware lets both reach views, exactly as the
    # middleware suites set up.
    enrol_totp(firm_user)
    enrol_totp(portal_user)
    Membership.objects.create(
        user=firm_user,
        tenant=tenant,
        role=TenantRole.STAFF_ACCOUNTANT,
        client=None,
    )
    Membership.objects.create(
        user=portal_user,
        tenant=tenant,
        role=TenantRole.CLIENT_OWNER,
        client=company,
    )
    return Firm(tenant, company, firm_user, portal_user)


def _enable(tenant: Tenant, *flags: str) -> None:
    tenant.features = [*tenant.features, *flags]
    tenant.save(update_fields=["features"])


def test_the_flag_registry_holds_exactly_the_r1_surfaces() -> None:
    # Pinned so a new surface cannot slip in uncommented and every later todo knows
    # the slugs its decorator accepts.
    assert (
        frozenset(
            {
                "cycles_r1",
                "briefing_r1",
                "ai_shadow",
                "bulk_r1",
                "import_r1",
                "firm_settings_r1",
                "scan_required",
                "federal_checks",
                "document_capture",
                "municipal_work",
            }
        )
        == FEATURES
    )


def test_new_tenants_start_with_every_flag_off() -> None:
    # Given a tenant created with no flags — the default state
    tenant = Tenant.objects.create(name="Beta", slug="beta")

    # Then the columns exist with the dark-by-default values
    assert tenant.features == []
    assert tenant.active_client_limit is None
    assert tenant_has(tenant.pk, "cycles_r1") is False


def test_tenant_has_reads_the_row_once_when_uncached(firm: Firm) -> None:
    # Given a flag stored on the tenant row
    _enable(firm.tenant, "cycles_r1")

    # Then tenant_has answers True from a single read
    assert tenant_has(firm.tenant.pk, "cycles_r1") is True
    assert tenant_has(firm.tenant.pk, "briefing_r1") is False


def test_tenant_has_is_cached_for_the_rest_of_a_request(
    firm: Firm,
    django_assert_num_queries: DjangoAssertNumQueries,
) -> None:
    # Given a flag stored on the tenant row
    _enable(firm.tenant, "cycles_r1")

    # When a flag cache is open — exactly what TenantMiddleware opens per request —
    # the first lookup reads the row and every later one is served from the cache
    with open_flag_cache():
        with django_assert_num_queries(1):
            assert tenant_has(firm.tenant.pk, "cycles_r1") is True
        with django_assert_num_queries(0):
            assert tenant_has(firm.tenant.pk, "cycles_r1") is True
            assert tenant_has(firm.tenant.pk, "briefing_r1") is False


def test_the_firm_gate_performs_no_flag_query_inside_the_request(
    firm: Firm,
) -> None:
    # Given a flagged tenant and a signed-in firm member
    _enable(firm.tenant, "cycles_r1")
    http = Client()
    http.force_login(firm.firm_user)

    # When a view asks tenant_has for its own tenant from inside the request — where
    # TenantMiddleware has already primed the cache from the row it resolved — the
    # count is observed rather than assumed
    response = http.get("/flagged-queries", headers={"host": FIRM_HOST})
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["has"] is True
    assert body["queries"] == 0


def test_an_unknown_flag_raises_rather_than_answering_false(
    firm: Firm,
) -> None:
    # A typo'd slug must never pass as "flag off": that answer would hide a
    # permanently broken gate behind a plausible 404, the same contract
    # UnknownCapability gives the permission matrix.
    with pytest.raises(UnknownFeature):
        tenant_has(firm.tenant.pk, "cyrcles_r1")
    with pytest.raises(UnknownFeature):
        require_feature("cyrcles_r1")


def test_a_flagged_view_404s_on_the_firm_host_when_the_flag_is_off(
    firm: Firm,
) -> None:
    # Given an authenticated firm member and NO flag on the tenant
    http = Client()
    http.force_login(firm.firm_user)

    # Then the flagged view is absent — 404, not 403: an unflagged tenant must not
    # learn the surface exists.
    response = http.get("/flagged", headers={"host": FIRM_HOST})
    assert response.status_code == HTTPStatus.NOT_FOUND


def test_a_flagged_view_404s_on_the_portal_host_when_the_flag_is_off(
    firm: Firm,
) -> None:
    # Given an authenticated portal user and NO flag on the tenant
    http = Client()
    http.force_login(firm.portal_user)

    # Then the portal variant is absent too, answered from the stash
    response = http.get("/flagged-portal", headers={"host": PORTAL_HOST})
    assert response.status_code == HTTPStatus.NOT_FOUND


def test_a_flagged_view_serves_on_both_hosts_when_the_flag_is_on(
    firm: Firm,
) -> None:
    # Given the tenant carries the flag
    _enable(firm.tenant, "cycles_r1")

    # Then the firm host serves the view
    http = Client()
    http.force_login(firm.firm_user)
    assert http.get("/flagged", headers={"host": FIRM_HOST}).status_code == (
        HTTPStatus.OK
    )

    # And the portal host serves the portal variant
    portal = Client()
    portal.force_login(firm.portal_user)
    assert portal.get("/flagged-portal", headers={"host": PORTAL_HOST}).status_code == (
        HTTPStatus.OK
    )


def test_the_kill_switch_overrides_a_flag_the_tenant_carries(
    firm: Firm,
    settings: SettingsWrapper,
) -> None:
    # Given a flagged tenant AND the flag killed platform-wide
    _enable(firm.tenant, "cycles_r1")
    settings.AXORIM_DISABLED_FEATURES = ["cycles_r1"]

    # Then every surface refuses, firm-side and portal-side and tenant_has itself
    assert tenant_has(firm.tenant.pk, "cycles_r1") is False
    http = Client()
    http.force_login(firm.firm_user)
    assert http.get("/flagged", headers={"host": FIRM_HOST}).status_code == (
        HTTPStatus.NOT_FOUND
    )
    portal = Client()
    portal.force_login(firm.portal_user)
    assert portal.get("/flagged-portal", headers={"host": PORTAL_HOST}).status_code == (
        HTTPStatus.NOT_FOUND
    )


def test_the_kill_switch_leaves_other_flags_alone(
    firm: Firm,
    settings: SettingsWrapper,
) -> None:
    # Given two flags on the tenant and only one killed
    _enable(firm.tenant, "cycles_r1", "briefing_r1")
    settings.AXORIM_DISABLED_FEATURES = ["briefing_r1"]

    # Then only the killed flag is off
    assert tenant_has(firm.tenant.pk, "cycles_r1") is True
    assert tenant_has(firm.tenant.pk, "briefing_r1") is False


def test_the_portal_flag_check_performs_no_query_after_the_role_switch(
    firm: Firm,
) -> None:
    # Given a flagged tenant and a signed-in portal user
    _enable(firm.tenant, "cycles_r1")
    http = Client()
    http.force_login(firm.portal_user)

    # When a view asks tenant_has for the request's own tenant from INSIDE the portal
    # transaction — where current_user is already app_portal and tenants_tenant is
    # denied to it — the count is observed rather than assumed
    response = http.get("/flagged-queries", headers={"host": PORTAL_HOST})
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["has"] is True
    assert body["queries"] == 0


def test_a_stash_built_for_another_tenant_refuses_to_answer(firm: Firm) -> None:
    # Given a stash resolved for the OTHER firm, as a live portal request for one
    # firm being asked about a second would hold
    other = Tenant.objects.create(name="Outra", slug="outra", features=["cycles_r1"])
    stash = PortalStash(
        user_pk=firm.portal_user.pk,
        tenant_id=other.pk,
        levels={},
        features=frozenset({"cycles_r1"}),
    )
    token = portal_stash.set(stash)
    try:
        # Then the question is refused, mirroring stashed_level's tenant rule
        with pytest.raises(LookupError):
            tenant_has(firm.tenant.pk, "cycles_r1")
    finally:
        portal_stash.reset(token)


def test_tenant_admin_writes_a_platform_event_for_the_change(firm: Firm) -> None:
    # Given a platform superuser (the console's editor role)
    operator = User.objects.create_superuser(
        email="root@platform.example",
        password=PASSWORD,
    )
    enrol_totp(operator)
    http = Client()
    http.force_login(operator)

    # When the operator enables a flag through the admin change form, as the cohort
    # runbook does
    response = http.post(
        f"/admin/tenants/tenant/{firm.tenant.pk}/change/",
        {
            "name": firm.tenant.name,
            "slug": firm.tenant.slug,
            "plan": firm.tenant.plan,
            "is_active": "on",
            "features": "cycles_r1,briefing_r1",
            "active_client_limit": "25",
        },
        headers={"host": "localhost"},
    )

    # Then the row carries the new entitlements AND a platform event records who
    # changed them, which tenant and which fields — a flag flip is exactly the kind
    # of action an investigation asks about.
    assert response.status_code == HTTPStatus.FOUND, response.content.decode()[:2000]
    firm.tenant.refresh_from_db()
    assert firm.tenant.features == ["cycles_r1", "briefing_r1"]
    assert firm.tenant.active_client_limit == 25
    event = PlatformEvent.objects.get(action=AuditAction.TENANT_UPDATED)
    assert event.actor_id == operator.pk
    assert event.tenant_id == firm.tenant.pk
    assert event.subject == firm.tenant.slug
    assert "features" in event.metadata["changed_fields"]


def test_tenant_admin_rejects_a_flag_the_registry_does_not_define(
    firm: Firm,
) -> None:
    # Given the same superuser posting a typo'd flag
    operator = User.objects.create_superuser(
        email="root@platform.example",
        password=PASSWORD,
    )
    enrol_totp(operator)
    http = Client()
    http.force_login(operator)

    # When the change form names a slug FEATURES does not define
    response = http.post(
        f"/admin/tenants/tenant/{firm.tenant.pk}/change/",
        {
            "name": firm.tenant.name,
            "slug": firm.tenant.slug,
            "plan": firm.tenant.plan,
            "is_active": "on",
            "features": "cyrcles_r1",
            "active_client_limit": "",
        },
        headers={"host": "localhost"},
    )

    # Then it is a field error, not a stored dead flag
    assert response.status_code == HTTPStatus.OK
    firm.tenant.refresh_from_db()
    assert firm.tenant.features == []
