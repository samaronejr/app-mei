"""Both roles are unable to change history; the minimized envelope is inspectable."""

import pytest
from django.db import DatabaseError, connection, transaction

from apps.core.tenancy import tenant_context
from apps.cycles.models import CycleEvent
from apps.cycles.services.cycle import add_note, open_cycle
from tests.cycles.cycle_fixtures import PERIOD, cycle_setup

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("role", ["app_runtime", "app_test"])
@pytest.mark.parametrize(
    "statement",
    ["UPDATE cycles_cycleevent SET type='forged'", "DELETE FROM cycles_cycleevent"],
)
def test_cycle_event_append_only(role: str, statement: str) -> None:
    setup = cycle_setup()
    with tenant_context(setup.tenant.pk):
        ref = open_cycle(
            setup.actor,
            setup.engagement.pk,
            PERIOD,
            setup.definition.pk,
            idempotency_key="not stored verbatim",
        )
        add_note(
            setup.actor, ref.id, "private document filename.pdf", expected_version=1
        )
        assert CycleEvent.objects.count() == 2
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL ROLE {role}")
            with pytest.raises(DatabaseError), transaction.atomic():
                cursor.execute(statement)
        assert CycleEvent.objects.count() == 2
        events = list(CycleEvent.objects.all())
        assert "filename.pdf" not in str([event.payload for event in events])
        assert "not stored verbatim" not in str([event.payload for event in events])
        assert all(
            event.schema_version == 1
            and event.aggregate_version >= 1
            and event.correlation_id
            and event.occurred_at
            and event.observed_at
            for event in events
        )
