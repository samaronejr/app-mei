"""T14 recovery: a waiver survives sweeps and rollback never opens portal access."""

from datetime import timedelta
from io import StringIO
from uuid import UUID

import pytest
from django.core.management import call_command
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor

from apps.core.tenancy import tenant_context
from apps.cycles.models import CycleEvent, CycleState, ExceptionState, ServiceCycle
from apps.cycles.models import Exception as CycleException
from apps.cycles.openers import CERTIFICATE_EXPIRING, evaluate_tenant_exceptions
from apps.cycles.services.exceptions import transition_exception
from tests.cycles.cycle_fixtures import CycleSetup, cycle_setup
from tests.cycles.test_exceptions import TODAY, _cycle, _open, _writes

pytestmark = pytest.mark.django_db(transaction=True)


def _portal_ids(setup: CycleSetup) -> set[UUID]:
    with (
        tenant_context(setup.tenant.pk),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute("SET LOCAL ROLE app_portal")
        cursor.execute(
            "SELECT set_config('app.client_id', %s, true)", [str(setup.client.pk)]
        )
        cursor.execute(
            "SELECT current_user, "
            "has_table_privilege(current_user, 'cycles_exception', 'SELECT'), "
            "has_table_privilege(current_user, 'cycles_exception', 'UPDATE'), "
            "has_table_privilege(current_user, 'cycles_exception', 'DELETE')"
        )
        assert cursor.fetchone() == ("app_portal", True, False, False)
        cursor.execute("SELECT id FROM cycles_exception")
        return {row[0] for row in cursor.fetchall()}


def test_an_accepted_risk_is_not_reopened_by_the_next_daily_sweep() -> None:
    setup = cycle_setup()
    with tenant_context(setup.tenant.pk):
        _cycle(setup)
        setup.client.certificate_expires_on = TODAY + timedelta(days=30)
        setup.client.save(update_fields=["certificate_expires_on"])
        evaluate_tenant_exceptions(setup.tenant, today=TODAY)
        problem = CycleException.objects.get()
        transition_exception(
            setup.actor, problem.pk, ExceptionState.HANDLING, expected_version=1
        )
        transition_exception(
            setup.actor,
            problem.pk,
            ExceptionState.RISK_ACCEPTED,
            expected_version=2,
            note="Renovação agendada e risco assumido.",
        )
        before = _writes()

        evaluate_tenant_exceptions(setup.tenant, today=TODAY + timedelta(days=1))

        assert _writes() == before


@pytest.mark.parametrize(
    "state", [CycleState.COMPLETED, CycleState.WITH_PENDING, CycleState.CANCELLED]
)
def test_the_sweep_never_adds_exceptions_to_a_closed_cycle(state: str) -> None:
    setup = cycle_setup()
    with tenant_context(setup.tenant.pk):
        cycle = _cycle(setup)
        setup.client.certificate_expires_on = TODAY
        setup.client.save(update_fields=["certificate_expires_on"])
        ServiceCycle.objects.filter(pk=cycle.pk).update(state=state)
        before = _writes()

        counts = evaluate_tenant_exceptions(setup.tenant, today=TODAY)

        assert counts.evaluated == 0
        assert _writes() == before


def test_populated_reverse_stays_portal_denied_after_grant_repair() -> None:
    setup = cycle_setup()
    with tenant_context(setup.tenant.pk):
        cycle = _cycle(setup)
        visible = _open(setup, cycle, CERTIFICATE_EXPIRING, setup.client.pk)
        hidden = _open(setup, cycle, "delivery_unknown", cycle.pk)
        exception_ids = {visible.pk, hidden.pk}
        event_ids = set(CycleEvent.objects.values_list("pk", flat=True))
        assert set(CycleException.objects.values_list("pk", flat=True)) == exception_ids
    assert _portal_ids(setup) == {visible.pk}
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
    try:
        # The actual migrate command invokes the normal post_migrate grant repair.
        # A REVOKE alone is insufficient with the current roles.sql SELECT grant.
        call_command(
            "migrate",
            "cycles",
            "0005_document_review",
            interactive=False,
            verbosity=0,
            stdout=StringIO(),
        )
        with tenant_context(setup.tenant.pk):
            assert set(CycleException.objects.values_list("pk", flat=True)) == (
                exception_ids
            )
            assert set(CycleEvent.objects.values_list("pk", flat=True)) == event_ids
        assert _portal_ids(setup) == set()
        call_command(
            "migrate", "cycles", interactive=False, verbosity=0, stdout=StringIO()
        )
        with tenant_context(setup.tenant.pk):
            assert set(CycleException.objects.values_list("pk", flat=True)) == (
                exception_ids
            )
            assert not CycleException.objects.filter(client_visible=True).exists()
            assert set(CycleEvent.objects.values_list("pk", flat=True)) == event_ids
        assert _portal_ids(setup) == set()
        # Reintroduce approved client copy after the destructive field rollback.
        # The positive control proves the restored policy is not simply deny-all.
        with tenant_context(setup.tenant.pk):
            CycleException.objects.filter(pk=visible.pk).update(
                client_visible=True, client_message=visible.client_message
            )
        assert _portal_ids(setup) == {visible.pk}
    finally:
        MigrationExecutor(connection).migrate(leaves)
        with connection.cursor() as cursor:
            cursor.execute("GRANT SELECT ON cycles_exception TO app_portal")
            cursor.execute("SET ROLE app_runtime")
