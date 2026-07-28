"""The checklist arrives with the client, and is driven by rows rather than by code.

The test that matters most is `test_adding_a_template_row_adds_it_to_new_clients`:
without it, "data-driven" is a claim about the shape of the code rather than a property
anyone has checked, and the first Phase 3 connector would discover it is false.
"""

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.clients.models import (
    ClientCompany,
    OnboardingItem,
    OnboardingItemTemplate,
    OnboardingStatus,
)
from apps.clients.services import create_default_checklist
from apps.core.tenancy import tenant_context
from apps.tenants.models import Tenant
from tests.isolation.rolecheck import assert_isolated_role

pytestmark = pytest.mark.django_db

DEFAULT_ITEM_COUNT = 6


@pytest.fixture
def tenant() -> Tenant:
    return Tenant.objects.create(name="Alpha", slug="alpha")


def _client(tenant: Tenant, cnpj: str = "AAAAAAAAAAAA01") -> ClientCompany:
    with tenant_context(tenant.id):
        return ClientCompany.objects.create(
            tenant=tenant,
            legal_name="Padaria do Ze MEI",
            cnpj=cnpj,
        )


def _settle(client: ClientCompany, status: str = OnboardingStatus.DONE) -> None:
    with tenant_context(client.tenant_id):
        client.onboarding_items.update(status=status, completed_at=timezone.now())


def test_registering_a_client_creates_the_default_checklist(tenant: Tenant) -> None:
    # Given the seeded template rows
    assert_isolated_role()
    assert OnboardingItemTemplate.objects.count() == DEFAULT_ITEM_COUNT

    # When a client is registered
    client = _client(tenant)

    # Then it already carries one item per active template row, in order
    with tenant_context(tenant.id):
        items = list(client.onboarding_items.all())
    assert len(items) == DEFAULT_ITEM_COUNT
    assert [item.key for item in items] == list(
        OnboardingItemTemplate.objects.values_list("key", flat=True),
    )
    assert {item.status for item in items} == {OnboardingStatus.PENDING}


def test_adding_a_template_row_adds_it_to_new_clients(tenant: Tenant) -> None:
    # Given an extra checklist row, added the way a Phase 3 connector would add one
    assert_isolated_role()
    OnboardingItemTemplate.objects.create(
        key="nfse_connector",
        label="Conector de NFS-e municipal configurado",
        position=99,
    )

    # When a client is registered afterwards
    client = _client(tenant)

    # Then the new item is on its checklist, with no migration and no code change
    with tenant_context(tenant.id):
        keys = set(client.onboarding_items.values_list("key", flat=True))
    assert "nfse_connector" in keys
    assert len(keys) == DEFAULT_ITEM_COUNT + 1


def test_an_inactive_template_row_is_not_handed_to_new_clients(tenant: Tenant) -> None:
    # Given a retired checklist row
    assert_isolated_role()
    OnboardingItemTemplate.objects.filter(key="invoice_model").update(is_active=False)

    # When a client is registered
    client = _client(tenant)

    # Then the retired item is absent. Retiring a row must not mean deleting it —
    # clients already holding that item keep their history.
    with tenant_context(tenant.id):
        keys = set(client.onboarding_items.values_list("key", flat=True))
    assert "invoice_model" not in keys
    assert len(keys) == DEFAULT_ITEM_COUNT - 1


def test_a_fresh_client_is_not_ready(tenant: Tenant) -> None:
    # Given a newly registered client
    assert_isolated_role()
    client = _client(tenant)

    # When readiness is derived
    # Then it is False: every item is still pending
    with tenant_context(tenant.id):
        assert client.is_ready is False


def test_completing_every_item_flips_is_ready(tenant: Tenant) -> None:
    # Given a client whose checklist is fully settled
    assert_isolated_role()
    client = _client(tenant)
    _settle(client)

    # When readiness is derived
    # Then it is True
    with tenant_context(tenant.id):
        assert client.is_ready is True


def test_a_not_applicable_item_does_not_hold_readiness_back(tenant: Tenant) -> None:
    # Given a client with every item done except one marked not applicable
    assert_isolated_role()
    client = _client(tenant)
    _settle(client)
    with tenant_context(tenant.id):
        client.onboarding_items.filter(key="invoice_model").update(
            status=OnboardingStatus.NOT_APPLICABLE,
            completed_at=None,
        )

        # When readiness is derived
        # Then it is still True — "does not apply" is settled, not outstanding
        assert client.is_ready is True


def test_a_client_with_no_checklist_at_all_is_not_ready(tenant: Tenant) -> None:
    # Given a client whose checklist was removed
    assert_isolated_role()
    client = _client(tenant)
    with tenant_context(tenant.id):
        client.onboarding_items.all().delete()

        # When readiness is derived
        # Then it is False. The vacuous reading — nothing outstanding, therefore
        # ready — would report a client nobody assessed as fit to file for.
        assert client.is_ready is False


def test_a_blocked_item_holds_readiness_back(tenant: Tenant) -> None:
    # Given a client whose checklist is otherwise complete
    assert_isolated_role()
    client = _client(tenant)
    _settle(client)

    # When one item is blocked
    with tenant_context(tenant.id):
        client.onboarding_items.filter(key="govbr_trust_level").update(
            status=OnboardingStatus.BLOCKED,
            blocked_reason="Conta gov.br ainda em nível bronze.",
            completed_at=None,
        )

        # Then the client is not ready, and the reason is on the record
        assert client.is_ready is False
        blocked = client.onboarding_items.get(key="govbr_trust_level")
    assert blocked.blocked_reason


def test_blocking_an_item_without_a_reason_is_rejected(tenant: Tenant) -> None:
    # Given a client's checklist
    assert_isolated_role()
    client = _client(tenant)

    # When an item is blocked with no explanation
    # Then the database refuses it. A client stuck with nobody able to say on what is
    # exactly the state this table exists to prevent.
    with (
        tenant_context(tenant.id),
        pytest.raises(IntegrityError, match="onboardingitem_blocked_needs_a_reason"),
        transaction.atomic(),
    ):
        client.onboarding_items.filter(key="tax_profile").update(
            status=OnboardingStatus.BLOCKED,
            blocked_reason="",
        )


def test_the_checklist_service_is_idempotent_per_client(tenant: Tenant) -> None:
    # Given a client that already has its checklist
    assert_isolated_role()
    client = _client(tenant)

    # When the checklist is generated a second time
    # Then the unique key refuses the duplicate rather than doubling the list
    with (
        tenant_context(tenant.id),
        pytest.raises(IntegrityError, match="onboardingitem_tenant_client_key_uniq"),
        transaction.atomic(),
    ):
        create_default_checklist(client)


def test_two_firms_checklists_do_not_leak_into_each_other(tenant: Tenant) -> None:
    # Given two firms each holding one client
    assert_isolated_role()
    beta = Tenant.objects.create(name="Beta", slug="beta")
    _client(tenant)
    _client(beta, cnpj="BBBBBBBBBBBB01")

    # When each firm counts its own onboarding items
    with tenant_context(tenant.id):
        alpha_items = OnboardingItem.objects.count()
    with tenant_context(beta.id):
        beta_items = OnboardingItem.objects.count()

    # Then neither sees the other's, at the row layer as well as the ORM's
    assert alpha_items == DEFAULT_ITEM_COUNT
    assert beta_items == DEFAULT_ITEM_COUNT
