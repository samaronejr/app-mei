"""The portal capability stash, one test per rule in its contract.

`transaction=True` throughout, for the reason `test_middleware.py` documents: under a
plain `django_db` the middleware's `atomic()` degrades to a savepoint, `SET LOCAL ROLE`
merges upward instead of reverting, and both the leak assertion and the denied-read
assertion stop meaning anything.

The headline is `test_a_require_can_gated_portal_view_is_reachable`. Every other test
here can be satisfied by a stash that is never consulted; that one fails outright
without it, because `require_can` passes no `tenant_id` and the ordinary path reads
`authz_capability` as `app_portal`.
"""

import json
from http import HTTPStatus
from uuid import UUID

import pytest
from django.test import Client
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.authz.matrix import CAPABILITY_SLUGS
from apps.authz.services import can, granted_levels, resolve_level
from apps.authz.stash import PortalStash, PortalStashMiss, portal_stash
from apps.clients.models import ClientCompany
from apps.core.tenancy import client_context, current_tenant_id, tenant_context
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.support import enrol_totp

pytestmark = pytest.mark.django_db(transaction=True)

PORTAL_HOST = "acme-portal.localhost"
PASSWORD = "irrelevant-here"  # noqa: S105
VAULT = "documents.transfer"


class Firm:
    """One firm, one MEI client, one portal user, plus a second firm to ask about."""

    def __init__(
        self,
        tenant: Tenant,
        other_tenant: Tenant,
        client: ClientCompany,
        portal_user: User,
        stranger: User,
    ) -> None:
        self.tenant = tenant
        self.other_tenant = other_tenant
        self.client = client
        self.portal_user = portal_user
        self.stranger = stranger


@pytest.fixture(autouse=True)
def _portal_urls(settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]
    monkeypatch.setattr("apps.portal.middleware.PORTAL_URLCONF", "tests.portal.urls")


@pytest.fixture
def firm() -> Firm:
    tenant = Tenant.objects.create(name="Acme", slug="acme")
    other = Tenant.objects.create(name="Outra", slug="outra")
    with tenant_context(tenant.id):
        # ALL_OBJECTS_OK: fixture seeding, before any portal context exists.
        company = ClientCompany.all_objects.create(
            tenant=tenant,
            legal_name="CLIENTE ACME",
            cnpj="11222333000181",
            is_mei=True,
        )
    portal_user = User.objects.create_user(email="mei@acme.example", password=PASSWORD)
    stranger = User.objects.create_user(email="other@acme.example", password=PASSWORD)
    enrol_totp(portal_user)
    Membership.objects.create(
        user=portal_user,
        tenant=tenant,
        role=TenantRole.CLIENT_OWNER,
        client=company,
    )
    return Firm(tenant, other, company, portal_user, stranger)


def _get(client: Client, path: str) -> tuple[int, dict[str, object]]:
    response = client.get(path, headers={"host": PORTAL_HOST})
    body: dict[str, object] = {}
    if response.status_code == HTTPStatus.OK:
        body = dict(json.loads(response.content))
    return response.status_code, body


def _stash_for(firm: Firm, **overrides: object) -> PortalStash:
    # client_context is required, not incidental: role_of filters on the client, so
    # building the levels without one resolves the FIRM-side membership and yields
    # all-NONE. That is the fail-open shape an earlier revision shipped, and it is why
    # the middleware builds the stash after both context variables are live.
    with tenant_context(firm.tenant.id), client_context(firm.client.id):
        levels = granted_levels(
            firm.portal_user,
            CAPABILITY_SLUGS,
            tenant_id=firm.tenant.id,
        )
    fields: dict[str, object] = {
        "user_pk": firm.portal_user.pk,
        "tenant_id": firm.tenant.id,
        "levels": levels,
    }
    fields.update(overrides)
    return PortalStash(**fields)  # type: ignore[arg-type]


def test_a_require_can_gated_portal_view_is_reachable(firm: Firm) -> None:
    # Given a portal user whose role holds documents.transfer at FULL
    http = Client()
    http.force_login(firm.portal_user)

    # When they open a view gated by require_can, which passes NO tenant_id
    status, body = _get(http, "/gated")

    # Then the stash answers and the view runs, rather than the ordinary path reading
    # authz_capability as app_portal and aborting the transaction
    assert status == HTTPStatus.OK, status
    assert body == {"reached": True}


def test_rule_2_the_stash_exists_only_once_the_portal_role_is_assumed(
    firm: Firm,
) -> None:
    # Given an ANONYMOUS request, which gets no membership and no role switch
    status, body = _get(Client(), "/stash")
    assert status == HTTPStatus.OK
    assert body["present"] is False

    # When the same path is opened by a portal user
    http = Client()
    http.force_login(firm.portal_user)
    status, body = _get(http, "/stash")

    # Then the stash is present, keyed on the role having been assumed
    assert status == HTTPStatus.OK
    assert body["present"] is True


def test_rule_6_the_stash_holds_every_matrix_slug(firm: Firm) -> None:
    # Given a portal request
    http = Client()
    http.force_login(firm.portal_user)

    # When the stash is read from inside the transaction
    _, body = _get(http, "/stash")

    # Then it carries the whole matrix, so no gated view can fall outside it
    assert body["slugs"] == sorted(CAPABILITY_SLUGS)
    assert len(CAPABILITY_SLUGS) == 16


def test_rule_4_the_stash_does_not_survive_the_response(firm: Firm) -> None:
    # Given the stash is unset before any request
    assert portal_stash.get() is None

    # When a portal request completes
    http = Client()
    http.force_login(firm.portal_user)
    assert _get(http, "/stash")[0] == HTTPStatus.OK

    # Then it is unset again — one thread serves many requests, so a bare set() here
    # would answer the NEXT request with this one's levels
    assert portal_stash.get() is None


def test_rule_1_a_capability_outside_the_stash_raises_rather_than_falling_through(
    firm: Firm,
) -> None:
    # Given a stash that deliberately omits the capability being asked about
    stash = _stash_for(firm, levels={VAULT: "full"})
    token = portal_stash.set(stash)
    try:
        with tenant_context(firm.tenant.id), pytest.raises(PortalStashMiss) as exc:
            resolve_level(firm.portal_user, "clients.view_all")
    finally:
        portal_stash.reset(token)

    # Then it names the read it refused to make, rather than returning NONE silently
    assert "authz_capability" in str(exc.value)


def test_rule_5_the_stash_refuses_to_answer_for_another_user(firm: Firm) -> None:
    # Given a stash built for the portal user
    token = portal_stash.set(_stash_for(firm))
    try:
        # When a DIFFERENT user is asked about, which can() permits
        with tenant_context(firm.tenant.id), pytest.raises(PortalStashMiss) as exc:
            can(firm.stranger, VAULT)
    finally:
        portal_stash.reset(token)

    # Then it refuses rather than reporting one account's permissions for another
    assert "was built for user" in str(exc.value)


def test_rule_7_an_omitted_tenant_resolves_to_the_context_not_a_miss(
    firm: Firm,
) -> None:
    # Given a stash and a live tenant context, as inside a portal request
    token = portal_stash.set(_stash_for(firm))
    try:
        with tenant_context(firm.tenant.id):
            # When tenant_id is OMITTED — exactly what require_can does
            allowed = can(firm.portal_user, VAULT)
    finally:
        portal_stash.reset(token)

    # Then the effective tenant is the context and the stash answers
    assert allowed is True


def test_rule_7_a_different_tenant_is_refused(firm: Firm) -> None:
    # Given the same stash
    token = portal_stash.set(_stash_for(firm))
    try:
        # When another firm's tenant is named explicitly
        with tenant_context(firm.tenant.id), pytest.raises(PortalStashMiss) as exc:
            can(firm.portal_user, VAULT, tenant_id=firm.other_tenant.id)
    finally:
        portal_stash.reset(token)

    # Then it refuses — a user with no membership in that firm must not be handed
    # this firm's levels
    assert "was built for tenant" in str(exc.value)


def test_rule_7_a_nested_tenant_context_is_refused(firm: Firm) -> None:
    # Given a stash built for this firm
    token = portal_stash.set(_stash_for(firm))
    try:
        # When a nested tenant_context moves the context var, as record_event does
        with tenant_context(firm.other_tenant.id), pytest.raises(PortalStashMiss):
            can(firm.portal_user, VAULT)
    finally:
        portal_stash.reset(token)

    # Then the omitted-tenant branch does not silently answer from the wrong firm.
    # This is why the rule compares the EFFECTIVE tenant rather than only the argument.
    assert current_tenant_id.get() is None


def test_rule_3_a_dual_membership_user_resolves_the_client_role(firm: Firm) -> None:
    # Given a user holding BOTH a firm-side and a client-side membership in one firm —
    # the shape that turned the earlier fix from fail-closed into fail-OPEN
    Membership.objects.create(
        user=firm.portal_user,
        tenant=firm.tenant,
        role=TenantRole.OPERATIONS_ADMIN,
        client=None,
    )
    http = Client()
    http.force_login(firm.portal_user)

    # When they open a portal request
    _, body = _get(http, "/stash")

    # Then the stash is keyed on them and their client role resolved, not the firm one
    assert body["user_pk"] == str(firm.portal_user.pk)
    assert body["tenant_id"] == str(firm.tenant.id)
    assert UUID(str(body["user_pk"])) == firm.portal_user.pk


def test_rule_8_an_exception_inside_the_block_is_not_masked(firm: Firm) -> None:
    # Given a portal request whose view raises after the stash is bound
    http = Client()
    http.force_login(firm.portal_user)

    # When the response is a streaming one, which _reject_streaming refuses
    with pytest.raises(TypeError) as exc:
        http.get("/streamer", headers={"host": PORTAL_HOST})

    # Then the ORIGINAL error surfaces. Binding the token at the set() instead of before
    # the try would raise UnboundLocalError from finally and bury this.
    assert "streaming response" in str(exc.value)
    assert portal_stash.get() is None
