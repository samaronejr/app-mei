"""The public projection is useful; actual portal SQL cannot read private content."""

import json
from dataclasses import dataclass
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import DatabaseError, IntegrityError, connection, transaction
from psycopg.sql import SQL, Identifier

from apps.core.tenancy import tenant_context
from apps.cycles.models import Exception as CycleException
from apps.cycles.models import ExceptionInternal, ServiceCycle
from apps.cycles.openers import TAX_PROFILE_UNKNOWN
from apps.cycles.services import exceptions
from apps.cycles.services.exceptions import ExceptionReason, open_exception
from tests.cycles.cycle_fixtures import PERIOD
from tests.cycles.test_exceptions import _open
from tests.portal.request_fixtures import PortalSetup, portal_setup

pytestmark = pytest.mark.django_db(transaction=True)
PRIVATE_ACTION = "PRIVATE-ACTION-MUST-NOT-CROSS-THE-DATABASE-BOUNDARY"
PUBLIC_MESSAGE = "PUBLIC-HELP-DIFFERENT-FROM-THE-PRIVATE-ACTION"
EVIDENCE_ID = UUID("2518b50d-65aa-4749-b53c-0e8cb37a9171")
REASON = "private-boundary-probe"


@dataclass(frozen=True, slots=True)
class PrivacyRows:
    world: PortalSetup
    other: PortalSetup
    visible: CycleException
    hidden: CycleException
    sibling: CycleException
    foreign: CycleException


def _visible(world: PortalSetup, cycle: ServiceCycle) -> CycleException:
    cycle.refresh_from_db()
    return open_exception(
        REASON,
        cycle.pk,
        actor=world.graph.actor,
        cycle_id=cycle.pk,
        expected_version=cycle.version,
        owner=world.graph.actor,
        review_on=PERIOD,
        evidence_refs=(EVIDENCE_ID,),
    )


@pytest.fixture
def rows(monkeypatch: pytest.MonkeyPatch) -> PrivacyRows:
    monkeypatch.setitem(
        exceptions._REASONS,
        REASON,
        ExceptionReason(
            PRIVATE_ACTION, client_visible=True, client_message=PUBLIC_MESSAGE
        ),
    )
    world, other = portal_setup("private-exceptions"), portal_setup("other-exceptions")
    with tenant_context(world.graph.tenant.pk):
        visible = _visible(world, world.cycle)
        hidden = _open(world.graph, world.cycle, TAX_PROFILE_UNKNOWN, world.cycle.pk)
        sibling = _visible(world, ServiceCycle.objects.get(client=world.other_client))
    with tenant_context(other.graph.tenant.pk):
        foreign = _visible(other, other.cycle)
    return PrivacyRows(world, other, visible, hidden, sibling, foreign)


def test_public_projection_contains_no_private_content(rows: PrivacyRows) -> None:
    with (
        tenant_context(rows.world.graph.tenant.pk),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        internal = ExceptionInternal.objects.get(exception=rows.visible)
        assert internal.next_action == PRIVATE_ACTION
        assert internal.evidence_refs == [str(EVIDENCE_ID)]
        cursor.execute("SET LOCAL ROLE app_portal")
        cursor.execute(
            "SELECT set_config('app.client_id', %s, true)",
            [str(rows.world.graph.client.pk)],
        )
        cursor.execute(
            "SELECT current_user, rolbypassrls, rolsuper "
            "FROM pg_roles WHERE rolname = current_user"
        )
        assert cursor.fetchone() == ("app_portal", False, False)
        cursor.execute("SELECT id, client_message FROM cycles_exception")
        assert cursor.fetchall() == [(rows.visible.pk, PUBLIC_MESSAGE)]
        cursor.execute("SELECT to_jsonb(problem)::text FROM cycles_exception problem")
        (payload,) = cursor.fetchone()
        assert PRIVATE_ACTION not in payload
        assert str(EVIDENCE_ID) not in payload
        assert {"next_action", "evidence_refs"}.isdisjoint(json.loads(payload))


@pytest.mark.parametrize("column", ["next_action", "evidence_refs"])
def test_private_sql_read_is_permission_denied(rows: PrivacyRows, column: str) -> None:
    with tenant_context(rows.world.graph.tenant.pk), connection.cursor() as cursor:
        assert ExceptionInternal.objects.filter(exception=rows.visible).exists()
        cursor.execute("SET LOCAL ROLE app_portal")
        cursor.execute(
            "SELECT set_config('app.client_id', %s, true)",
            [str(rows.world.graph.client.pk)],
        )
        with pytest.raises(DatabaseError) as refused, transaction.atomic():
            cursor.execute(
                SQL("SELECT {} FROM cycles_exceptioninternal").format(
                    Identifier(column)
                )
            )
        cause = refused.value.__cause__
        assert isinstance(cause, psycopg.Error)
        assert cause.sqlstate == "42501"


@pytest.mark.parametrize("context", ["own", "sibling", "foreign", "empty", "unset"])
def test_real_portal_context_limits_public_rows(
    rows: PrivacyRows, context: str
) -> None:
    clients = {
        "own": str(rows.world.graph.client.pk),
        "sibling": str(rows.world.other_client.pk),
        "foreign": str(rows.other.graph.client.pk),
        "empty": "",
    }
    expected = {
        "own": {rows.visible.pk},
        "sibling": {rows.sibling.pk},
        "foreign": set(),
        "empty": set(),
        "unset": set(),
    }
    # A fresh connection makes the unset-GUC case genuinely unset, not RESET's ''.
    with (
        psycopg.connect(**connection.get_connection_params()) as session,
        session.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT set_config('app.tenant_id', %s, true)",
            [str(rows.world.graph.tenant.pk)],
        )
        if context in clients:
            cursor.execute(
                "SELECT set_config('app.client_id', %s, true)", [clients[context]]
            )
        else:
            cursor.execute("SELECT current_setting('app.client_id', true)")
            assert cursor.fetchone() == (None,)
        cursor.execute("SET LOCAL ROLE app_portal")
        cursor.execute("SELECT current_user")
        assert cursor.fetchone() == ("app_portal",)
        cursor.execute("SELECT id FROM cycles_exception")
        assert {row[0] for row in cursor.fetchall()} == expected[context]


def test_bypass_control_proves_hidden_and_foreign_rows_exist(
    rows: PrivacyRows,
) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        # The pytest database is owned by app_test, not app_migrator.
        cursor.execute("SET LOCAL ROLE app_test")
        cursor.execute(
            "SELECT current_user, rolbypassrls FROM pg_roles WHERE rolname=current_user"
        )
        assert cursor.fetchone() == ("app_test", True)
        cursor.execute("SELECT id FROM cycles_exception")
        assert {row[0] for row in cursor.fetchall()} == {
            rows.visible.pk,
            rows.hidden.pk,
            rows.sibling.pk,
            rows.foreign.pk,
        }
        cursor.execute("SELECT exception_id FROM cycles_exceptioninternal")
        assert {row[0] for row in cursor.fetchall()} == {
            rows.visible.pk,
            rows.hidden.pk,
            rows.sibling.pk,
            rows.foreign.pk,
        }


@pytest.mark.parametrize("target", ["sibling", "foreign"])
def test_private_companion_cannot_name_another_clients_exception(
    rows: PrivacyRows, target: str
) -> None:
    problem = rows.sibling if target == "sibling" else rows.foreign
    with tenant_context(rows.world.graph.tenant.pk):
        with pytest.raises(IntegrityError), transaction.atomic():
            ExceptionInternal.objects.create(
                tenant=rows.world.graph.tenant,
                client=rows.world.graph.client,
                exception_id=problem.pk,
                next_action="forged",
                evidence_refs=[str(uuid4())],
            )
        assert ExceptionInternal.objects.count() == 3


def test_private_companions_remain_tenant_isolated(rows: PrivacyRows) -> None:
    with tenant_context(rows.world.graph.tenant.pk):
        visible = set(ExceptionInternal.objects.values_list("exception_id", flat=True))
        assert visible == {
            rows.visible.pk,
            rows.hidden.pk,
            rows.sibling.pk,
        }
        assert not ExceptionInternal.objects.filter(
            exception_id=rows.foreign.pk
        ).exists()
