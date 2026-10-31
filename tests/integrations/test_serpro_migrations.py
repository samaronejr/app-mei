"""AXRM-013 firm tables reverse while empty and refuse once they hold history."""

import json
from datetime import timedelta
from pathlib import Path

import pytest
from django.db import connection
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.integrations.serpro.authorization import TermoSubmission, submit_termo
from apps.integrations.serpro.models import (
    FirmIntegrationIssue,
    FirmProviderReservation,
)
from apps.integrations.serpro.qualification import SyntheticTermo, synthetic_termo
from apps.integrations.serpro.work import PROC_MISSING, own_firm_condition
from tests.fakes.provider import Reply
from tests.fakes.serpro import FakeSerpro
from tests.integrations.test_migration_rollback import _as_migrator

pytestmark = pytest.mark.django_db(transaction=True)

TABLES = ("integrations_firmintegrationissue", "integrations_firmproviderreservation")


def tables() -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relname FROM pg_class WHERE relname = ANY(%s) AND relkind = 'r'",
            [list(TABLES)],
        )
        return {row[0] for row in cursor.fetchall()}


@pytest.fixture
def world(settings: SettingsWrapper, tmp_path: Path) -> SyntheticTermo:
    settings.MEDIA_ROOT = tmp_path
    settings.PROVIDERS_PAUSED = False
    settings.SERPRO_PAUSED = False
    settings.SERPRO_ENVIRONMENT = "fake"
    settings.PROVIDER_GLOBAL_MONTHLY_CAP_BRL = {"serpro": "0"}
    return synthetic_termo("fake")


def test_empty_firm_tables_reverse_and_come_back_isolated() -> None:
    # Given no firm work or firm reservation exists yet.
    # When the schema owner reverses to the budget migration, then reapplies.
    with _as_migrator():
        MigrationExecutor(connection).migrate(
            [("integrations", "0004_provider_budgets")]
        )
        assert tables() == set()
    # Then both tables return with forced RLS, no portal read and runtime grants.
    assert tables() == set(TABLES)
    with connection.cursor() as cursor:
        for table in TABLES:
            cursor.execute(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = %s",
                [table],
            )
            assert cursor.fetchone() == (True, True)
            cursor.execute(
                "SELECT has_table_privilege('app_portal', %s, 'SELECT')", [table]
            )
            assert cursor.fetchone() == (False,)
            cursor.execute(
                "SELECT has_table_privilege('app_runtime', %s, privilege) "
                "FROM unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']) privilege",
                [table],
            )
            assert cursor.fetchall() == [(True,)] * 4


def test_owned_firm_work_refuses_the_reverse_and_survives(
    world: SyntheticTermo,
) -> None:
    # Given owned firm work that must outlive an operational rollback.
    with tenant_context(world.tenant.pk):
        own_firm_condition(world.connection, PROC_MISSING, [])
    # When the schema owner attempts to reverse past it, then it refuses.
    with _as_migrator(), pytest.raises(IrreversibleError, match="forward repair"):
        MigrationExecutor(connection).migrate(
            [("integrations", "0004_provider_budgets")]
        )
    with tenant_context(world.tenant.pk):
        assert FirmIntegrationIssue.objects.get().reason == PROC_MISSING


def test_firm_reservation_history_refuses_the_reverse_and_survives(
    world: SyntheticTermo,
) -> None:
    # Given one submitted termo whose zero-price attempt reservation was released.
    until = timezone.localdate() + timedelta(days=30)
    reply = Reply(
        body=json.dumps(
            {
                "status": 200,
                "mensagens": [],
                "dados": json.dumps(
                    {
                        "autenticar_procurador_token": "synthetic-migration-token",
                        "data_hora_expiracao": f"{until.isoformat()}T00:00:00",
                    }
                ),
            }
        ).encode()
    )
    with FakeSerpro((reply,)) as peer:
        result = submit_termo(
            world.actor,
            world.tenant.pk,
            TermoSubmission(
                world.connection.pk,
                world.document.pk,
                world.connection.version,
                world.parties,
                peer.origin,
            ),
        )
    assert result.state == "sucesso"
    # When the schema owner attempts to drop the reservation table, it refuses.
    with _as_migrator(), pytest.raises(IrreversibleError, match="forward repair"):
        MigrationExecutor(connection).migrate(
            [("integrations", "0005_firm_integration_issues")]
        )
    with tenant_context(world.tenant.pk):
        assert list(
            FirmProviderReservation.objects.values_list(
                "attempt_number", "microbrl", "state"
            )
        ) == [(1, 0, "released")]
