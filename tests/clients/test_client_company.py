"""The client registry's two structural guarantees, asserted against PostgreSQL.

Both cases here are about a boundary rather than about a field. A CNPJ unique across
the whole platform would let one firm's registry decide whether another firm may
onboard a company; a `(tenant, id)` constraint that failed to materialize would leave
every composite foreign key in later waves unbuildable.
"""

from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction

from apps.clients.models import ClientCompany, ClientStatus
from apps.core.tenancy import tenant_context
from apps.tenants.models import Tenant
from tests.isolation.rolecheck import assert_isolated_role

pytestmark = pytest.mark.django_db(transaction=True)

TABLE = ClientCompany._meta.db_table
SHARED_CNPJ = "12ABC34501DE35"


class TenantPair:
    """Two firms that will be shown to hold the same client number at once."""

    def __init__(self, alpha: Tenant, beta: Tenant) -> None:
        self.alpha = alpha
        self.beta = beta


@pytest.fixture
def tenants() -> TenantPair:
    return TenantPair(
        Tenant.objects.create(name="Alpha", slug="alpha"),
        Tenant.objects.create(name="Beta", slug="beta"),
    )


def _create(tenant: Tenant, **overrides: Any) -> ClientCompany:  # noqa: ANN401
    fields: dict[str, Any] = {
        "tenant": tenant,
        "legal_name": "Padaria do Ze MEI",
        "cnpj": SHARED_CNPJ,
    }
    fields.update(overrides)
    with tenant_context(tenant.id):
        return ClientCompany.objects.create(**fields)


def _index_definitions() -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = 'public' AND tablename = %s",
            [TABLE],
        )
        return [row[0] for row in cursor.fetchall()]


def test_the_same_cnpj_may_be_held_by_two_different_firms(
    tenants: TenantPair,
) -> None:
    # Given two firms
    assert_isolated_role()

    # When each registers a client carrying the same CNPJ
    alpha_client = _create(tenants.alpha)
    beta_client = _create(tenants.beta)

    # Then both rows exist. During a hand-over both firms hold the client at once, so
    # a platform-wide unique index would make the second firm's onboarding fail on
    # the strength of a row it is not allowed to see.
    assert alpha_client.pk != beta_client.pk
    assert alpha_client.cnpj == beta_client.cnpj
    with tenant_context(tenants.alpha.id):
        assert ClientCompany.objects.count() == 1
    with tenant_context(tenants.beta.id):
        assert ClientCompany.objects.count() == 1


def test_the_same_cnpj_twice_inside_one_firm_is_rejected(
    tenants: TenantPair,
) -> None:
    # Given a firm that already registered a client
    assert_isolated_role()
    _create(tenants.alpha)

    # When the same CNPJ is registered again under that same firm
    # Then the per-tenant unique constraint rejects it
    with pytest.raises(IntegrityError, match="clientcompany_tenant_cnpj_uniq"):
        _create(tenants.alpha, legal_name="Duplicada")


def test_the_composite_referent_for_later_foreign_keys_exists() -> None:
    # Given the registry table
    assert_isolated_role()

    # When PostgreSQL's constraint catalogue is read
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT contype FROM pg_constraint "
            "WHERE conrelid = %s::regclass AND conname = %s",
            [TABLE, "clientcompany_tenant_id_uniq"],
        )
        row = cursor.fetchone()

    # Then (tenant_id, id) is a real UNIQUE constraint. A unique *index* alone is not
    # interchangeable here: every tenant-crossing composite FK in T-026 and later
    # waves names these two columns as its referent.
    assert row is not None, "clientcompany_tenant_id_uniq is missing"
    assert row[0] == "u", "clientcompany_tenant_id_uniq is not a UNIQUE constraint"


@pytest.mark.parametrize("column", ["status", "cnpj"])
def test_every_composite_index_leads_with_the_tenant_column(column: str) -> None:
    # Given the registry's indexes
    assert_isolated_role()
    definitions = _index_definitions()
    assert definitions, "no indexes found — this check would pass vacuously"

    # When the ones covering this column are inspected
    matching = [d for d in definitions if f"tenant_id, {column}" in d]

    # Then one exists with tenant_id first. The row-level-security predicate filters
    # on tenant_id, so an index that does not lead with it stops serving the predicate.
    assert matching, (
        f"no index on ({column}) leads with tenant_id; definitions: {definitions}"
    )


def test_a_cnpj_that_is_not_normalized_is_rejected(tenants: TenantPair) -> None:
    # Given a firm
    assert_isolated_role()

    # When a punctuated, non-normalized CNPJ is stored
    # Then the shape constraint rejects it. `char(14)` would have padded it instead.
    with (
        pytest.raises(IntegrityError, match="clientcompany_cnpj_normalized"),
        transaction.atomic(),
    ):
        _create(tenants.alpha, cnpj="12.345.678/00")


def test_an_alphanumeric_cnpj_is_accepted(tenants: TenantPair) -> None:
    # Given a firm
    assert_isolated_role()

    # When a client is registered with letters in its CNPJ, as IN RFB nº 2.229/2024
    # requires from 2026-07-31
    client = _create(tenants.alpha, cnpj="AB123CD456EF78")

    # Then it stores unchanged. A digits-only column would have to be migrated away
    # before the product's first alphanumeric registration.
    assert client.cnpj == "AB123CD456EF78"
    assert client.status == ClientStatus.ONBOARDING
