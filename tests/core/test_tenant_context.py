"""Work outside the request cycle must establish tenant context, and give it back.

The post-loop assertions are the point of this file. Each iteration sets the context
correctly on entry, so a per-iteration check passes even when the block leaks — the
damage is only visible *after* the loop, which is exactly where a `PlatformTask`
sweeping every tenant would do it.
"""

import uuid

import pytest
from celery import shared_task
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction

from apps.core.management.base import TenantAwareBaseCommand
from apps.core.tasks import PlatformTask, TenantTask
from apps.core.tenancy import MissingTenantContext, current_tenant_id, tenant_context
from apps.core.tests.models import ExampleTenantModel
from apps.tenants.models import Tenant

pytestmark = pytest.mark.django_db(transaction=True)

ROWS_FOR_ALPHA = 3
ROWS_FOR_BETA = 2


def _read_guc() -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT coalesce(current_setting('app.tenant_id', true), '')")
        row = cursor.fetchone()
    return str(row[0]) if row else ""


@shared_task(base=TenantTask, bind=False)
def count_visible_rows(tenant_id: str) -> int:
    """Count what this tenant can see. tenant_id is consumed by the task base."""
    return ExampleTenantModel.objects.count()


@shared_task(base=PlatformTask, bind=True)
def count_every_tenant(self: PlatformTask) -> list[tuple[str, int]]:
    """Visit every tenant in turn, never with two in scope at once."""
    return [
        (tenant.slug, ExampleTenantModel.objects.count())
        for tenant in self.each_tenant()
    ]


class _ProbeCommand(TenantAwareBaseCommand):
    help = "Report what a single tenant can see."

    def handle_tenant(self, tenant: Tenant, *args: object, **options: object) -> str:
        return f"{tenant.slug}:{ExampleTenantModel.objects.count()}"


@pytest.fixture
def seeded() -> tuple[Tenant, Tenant]:
    alpha = Tenant.objects.create(name="Alpha", slug="alpha")
    beta = Tenant.objects.create(name="Beta", slug="beta")
    for tenant, count in ((alpha, ROWS_FOR_ALPHA), (beta, ROWS_FOR_BETA)):
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.tenant_id', %s, true)",
                [str(tenant.id)],
            )
            for index in range(count):
                ExampleTenantModel.all_objects.create(
                    tenant=tenant,
                    name=f"{tenant.slug}{index}",
                )
    return alpha, beta


def test_tenant_context_sets_both_layers(seeded: tuple[Tenant, Tenant]) -> None:
    # Given two tenants with rows apiece
    alpha, _ = seeded

    # When a block runs inside Alpha's context
    with tenant_context(alpha.id):
        # Then both the ContextVar and the database GUC hold Alpha
        assert current_tenant_id.get() == alpha.id
        assert _read_guc() == str(alpha.id)
        assert ExampleTenantModel.objects.count() == ROWS_FOR_ALPHA


def test_tenant_context_restores_both_layers_on_exit(
    seeded: tuple[Tenant, Tenant],
) -> None:
    # Given no context to begin with
    alpha, _ = seeded
    before = _read_guc()

    # When a context is entered and left
    with tenant_context(alpha.id):
        pass

    # Then both layers are back where they started
    assert current_tenant_id.get() is None
    assert _read_guc() == before


def test_a_nested_context_does_not_leak_the_inner_tenant(
    seeded: tuple[Tenant, Tenant],
) -> None:
    # Given an already-open transaction, so the inner atomic() is a SAVEPOINT
    alpha, beta = seeded
    with transaction.atomic(), tenant_context(alpha.id):
        assert _read_guc() == str(alpha.id)

        # When a nested context for another tenant opens and closes
        with tenant_context(beta.id):
            assert _read_guc() == str(beta.id)

        # Then the outer tenant is restored. PostgreSQL MERGES a SET LOCAL into the
        # parent on RELEASE SAVEPOINT, so without an explicit restore this would
        # still be Beta.
        assert _read_guc() == str(alpha.id)
        assert current_tenant_id.get() == alpha.id


def test_a_loop_over_tenants_does_not_leak_after_it_finishes(
    seeded: tuple[Tenant, Tenant],
) -> None:
    # Given an open transaction and the two tenants
    alpha, beta = seeded
    with transaction.atomic():
        before = _read_guc()

        # When each tenant is visited in turn
        for tenant in (alpha, beta):
            with tenant_context(tenant.id):
                assert _read_guc() == str(tenant.id)

        # Then nothing survives the loop. A per-iteration check alone would pass even
        # while leaking, because each iteration sets the value correctly on entry.
        assert _read_guc() == before
        assert current_tenant_id.get() is None


def test_tenant_context_refuses_to_run_without_a_tenant() -> None:
    # Given no tenant id
    # When a context is requested anyway
    # Then it fails loudly instead of silently scoping to nothing
    with pytest.raises(MissingTenantContext), tenant_context(None):
        pytest.fail("the body must never run")


def test_a_tenant_task_sees_only_its_own_rows(seeded: tuple[Tenant, Tenant]) -> None:
    # Given two tenants with different row counts
    alpha, beta = seeded

    # When the task runs eagerly for each
    alpha_result = count_visible_rows.apply(args=[str(alpha.id)])
    beta_result = count_visible_rows.apply(args=[str(beta.id)])

    # Then each sees exactly its own, and neither sees the total
    assert alpha_result.get() == ROWS_FOR_ALPHA
    assert beta_result.get() == ROWS_FOR_BETA
    assert ROWS_FOR_ALPHA != ROWS_FOR_ALPHA + ROWS_FOR_BETA


def test_a_tenant_task_without_context_raises_rather_than_returning_empty() -> None:
    # Given a tenant-scoped task invoked with no tenant
    result = count_visible_rows.apply(args=[None])

    # When its outcome is inspected
    # Then it failed loudly. An empty result would look like a successful sweep that
    # legitimately had nothing to do.
    assert result.failed()
    with pytest.raises(MissingTenantContext):
        result.get()


def test_a_platform_task_visits_each_tenant_separately(
    seeded: tuple[Tenant, Tenant],
) -> None:
    # Given two tenants
    assert seeded

    # When a platform task iterates them
    observed = count_every_tenant.apply().get()

    # Then it sees each tenant's own rows in turn, never the combined total
    assert observed == [("alpha", ROWS_FOR_ALPHA), ("beta", ROWS_FOR_BETA)]
    assert all(count != ROWS_FOR_ALPHA + ROWS_FOR_BETA for _, count in observed)


def test_a_platform_task_leaves_no_context_behind(
    seeded: tuple[Tenant, Tenant],
) -> None:
    # Given a completed platform sweep
    assert seeded
    count_every_tenant.apply().get()

    # When the ambient state is inspected afterwards
    # Then no tenant is still pinned from the final iteration
    assert current_tenant_id.get() is None
    assert _read_guc() == ""


def test_a_tenant_aware_command_runs_inside_the_tenants_context(
    seeded: tuple[Tenant, Tenant],
) -> None:
    # Given the probe command and a known tenant
    alpha, _ = seeded

    # When it is invoked with --tenant
    output = _ProbeCommand().handle(tenant_slug=alpha.slug)

    # Then it saw that tenant's rows, so context was established for it
    assert output == f"alpha:{ROWS_FOR_ALPHA}"


def test_a_tenant_aware_command_rejects_an_unknown_tenant() -> None:
    # Given a slug that matches no active tenant
    # When the command runs
    # Then it refuses rather than doing nothing quietly
    with pytest.raises(CommandError, match="No active tenant"):
        _ProbeCommand().handle(tenant_slug="does-not-exist")


def test_a_tenant_aware_command_requires_the_tenant_argument() -> None:
    # Given the command's own argument parser
    # When --tenant is omitted
    # Then argparse refuses, so the command can never run unscoped by accident
    with pytest.raises(CommandError, match="required: --tenant"):
        call_command(_ProbeCommand())


def test_tenant_context_rejects_a_non_uuid_tenant_id() -> None:
    # Given a task invoked with something that is not a tenant id
    result = count_visible_rows.apply(args=[12345])

    # When its outcome is inspected
    # Then it failed loudly rather than coercing to an unexpected scope
    assert result.failed()
    with pytest.raises(MissingTenantContext):
        result.get()


def test_a_random_uuid_yields_no_rows_rather_than_an_error(
    seeded: tuple[Tenant, Tenant],
) -> None:
    # Given a well-formed tenant id that belongs to nobody
    assert seeded

    # When a task runs under it
    result = count_visible_rows.apply(args=[str(uuid.uuid4())])

    # Then it succeeds and sees nothing, which is the fail-closed answer
    assert result.get() == 0
