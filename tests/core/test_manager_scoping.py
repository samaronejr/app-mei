"""The scoped manager must filter by the ContextVar, and fail closed without one.

Both layers are seeded here on purpose. A fixture that established only the GUC would
leave the ContextVar unset, so the scoped manager would return `none()` and the "sees
only tenant A" assertion would observe zero — a result that is indistinguishable from
correct and is also satisfied by a manager hard-coded to `return self.none()`.
"""

from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from django.db import connection, transaction

from apps.core.tenancy import current_tenant_id
from apps.core.tests.models import ExampleTenantModel
from apps.tenants.models import Tenant

pytestmark = pytest.mark.django_db(transaction=True)

ROWS_FOR_A = 3
ROWS_FOR_B = 2


def _set_guc(tenant_id: object) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant_id)])


@dataclass(frozen=True)
class Seeded:
    """Two tenants with rows apiece, and tenant A established in both layers."""

    tenant_a: Tenant
    tenant_b: Tenant


@pytest.fixture
def seeded() -> Iterator[Seeded]:
    tenant_a = Tenant.objects.create(name="Alpha", slug="alpha")
    tenant_b = Tenant.objects.create(name="Beta", slug="beta")

    # Each tenant's rows must be written under that tenant's own context: the forced
    # WITH CHECK policy rejects an insert carrying somebody else's tenant_id.
    with transaction.atomic():
        _set_guc(tenant_a.id)
        for index in range(ROWS_FOR_A):
            ExampleTenantModel.all_objects.create(tenant=tenant_a, name=f"a{index}")
    with transaction.atomic():
        _set_guc(tenant_b.id)
        for index in range(ROWS_FOR_B):
            ExampleTenantModel.all_objects.create(tenant=tenant_b, name=f"b{index}")

    with transaction.atomic():
        _set_guc(tenant_a.id)
        token = current_tenant_id.set(tenant_a.id)
        try:
            yield Seeded(tenant_a=tenant_a, tenant_b=tenant_b)
        finally:
            current_tenant_id.reset(token)


def test_the_scoped_manager_returns_exactly_the_tenant_in_context(
    seeded: Seeded,
) -> None:
    # Given tenant A in both the ContextVar and the GUC, with rows for A and for B
    # When the scoped manager counts rows
    count = ExampleTenantModel.objects.count()

    # Then it sees all of A's rows and none of B's. The strict inequality matters:
    # without it a manager returning none() unconditionally would satisfy this.
    assert count == ROWS_FOR_A
    assert count > 0
    assert ExampleTenantModel.objects.filter(tenant_id=seeded.tenant_b.id).count() == 0


def test_the_scoped_manager_never_yields_another_tenants_row(seeded: Seeded) -> None:
    # Given tenant A in context
    # When every visible row is inspected
    tenants_seen = set(ExampleTenantModel.objects.values_list("tenant_id", flat=True))

    # Then every one of them belongs to A
    assert tenants_seen == {seeded.tenant_a.id}


def test_the_manager_keys_on_the_contextvar_not_on_the_database_guc(
    seeded: Seeded,
) -> None:
    # Given the GUC still set to tenant A but the ContextVar cleared
    token = current_tenant_id.set(None)
    try:
        # When the scoped manager counts rows
        count = ExampleTenantModel.objects.count()
    finally:
        current_tenant_id.reset(token)

    # Then it fails closed at the application layer, independently of row-level
    # security, which would still have allowed all of A's rows through
    assert count == 0
    assert seeded.tenant_a is not None


def test_the_unscoped_manager_is_reachable_and_ignores_the_contextvar(
    seeded: Seeded,
) -> None:
    # Given tenant A in both layers
    # When the unscoped manager counts rows
    count = ExampleTenantModel.all_objects.count()

    # Then it is bounded by row-level security alone — A's rows, not B's — proving it
    # is genuinely unfiltered by the ContextVar rather than simply empty
    assert count == ROWS_FOR_A
    assert count > 0
    assert seeded.tenant_b is not None


def test_all_tenants_returns_an_unfiltered_queryset(seeded: Seeded) -> None:
    # Given tenant A in context
    # When the audited escape hatch is used
    # ALL_TENANTS_OK: proving the escape hatch is unscoped at the application layer.
    escaped = ExampleTenantModel.objects.all_tenants().count()

    # Then the ContextVar filter is gone. The database still bounds it to A, because
    # app.tenant_id is set — crossing tenants needs tenant_context per tenant too.
    assert escaped == ROWS_FOR_A
    assert seeded.tenant_a is not None


def test_all_tenants_is_not_filtered_when_the_contextvar_is_unset(
    seeded: Seeded,
) -> None:
    # Given no tenant in the ContextVar, but the GUC still on A
    token = current_tenant_id.set(None)
    try:
        # When the escape hatch and the scoped manager are compared
        # ALL_TENANTS_OK: contrasting the escape hatch against the fail-closed default.
        escaped = ExampleTenantModel.objects.all_tenants().count()
        scoped = ExampleTenantModel.objects.count()
    finally:
        current_tenant_id.reset(token)

    # Then the escape hatch still returns rows while the scoped manager returns none
    assert escaped == ROWS_FOR_A
    assert scoped == 0
    assert seeded.tenant_a is not None
