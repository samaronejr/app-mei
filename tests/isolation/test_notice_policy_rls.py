"""Non-vacuous runtime isolation and portal NONE for notice authority."""

import pytest
from django.db import ProgrammingError, connection, transaction

from apps.core.tenancy import tenant_context
from apps.delivery.models import TenantNoticePolicy
from tests.cycles.cycle_fixtures import CycleSetup, cycle_setup
from tests.delivery import notice_fixtures
from tests.delivery.notice_fixtures import acknowledge
from tests.isolation.rolecheck import assert_isolated_role

pytestmark = pytest.mark.django_db(transaction=True)
graph = notice_fixtures.graph


def test_notice_policy_is_forced_and_foreign_read_mutation_are_denied(
    graph: CycleSetup,
) -> None:
    assert_isolated_role()
    policy = acknowledge(graph)
    foreign = cycle_setup("notice-rls-foreign")
    with tenant_context(graph.tenant.pk):
        # ALL_OBJECTS_OK: positive runtime control bypasses only the ORM filter.
        assert TenantNoticePolicy.all_objects.filter(pk=policy.pk).count() == 1
    with tenant_context(foreign.tenant.pk):
        # ALL_OBJECTS_OK: these denials must be caused by RLS, not ContextVar.
        assert TenantNoticePolicy.all_objects.filter(pk=policy.pk).count() == 0
        assert (
            # ALL_OBJECTS_OK: prove the foreign UPDATE is denied by RLS itself.
            TenantNoticePolicy.all_objects.filter(pk=policy.pk).update(
                version=2, grants_json=[]
            )
            == 0
        )
    with tenant_context(graph.tenant.pk):
        policy.refresh_from_db()
        assert policy.version == policy.acknowledged_version == 1
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE oid = 'delivery_tenantnoticepolicy'::regclass"
        )
        assert cursor.fetchone() == (True, True)


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM delivery_tenantnoticepolicy WHERE id = %s",
        "UPDATE delivery_tenantnoticepolicy SET version = version + 1 WHERE id = %s",
        "DELETE FROM delivery_tenantnoticepolicy WHERE id = %s",
        (
            "INSERT INTO delivery_tenantnoticepolicy "
            "SELECT * FROM delivery_tenantnoticepolicy WHERE id = %s"
        ),
    ],
)
def test_portal_cannot_read_or_write_notice_policy(
    graph: CycleSetup,
    statement: str,
) -> None:
    assert_isolated_role()
    policy = acknowledge(graph)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE app_portal")
        cursor.execute(
            "SELECT set_config('app.tenant_id', %s, true), "
            "set_config('app.client_id', %s, true)",
            [str(graph.tenant.pk), str(graph.client.pk)],
        )
        with pytest.raises(ProgrammingError) as error:
            cursor.execute(statement, [str(policy.pk)])
        transaction.set_rollback(True)
    assert getattr(error.value.__cause__, "sqlstate", None) == "42501"
    with tenant_context(graph.tenant.pk):
        assert TenantNoticePolicy.objects.get(pk=policy.pk).version == 1
