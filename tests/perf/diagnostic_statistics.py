"""Temporary diagnostic: preserve old-tenant statistics across a fixture truncate."""

from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.db import connection
from pytest_django.fixtures import DjangoAssertNumQueries, SettingsWrapper

from tests.isolation.rolecheck import assert_isolated_role
from tests.perf import test_read_budgets

pytestmark = pytest.mark.django_db(transaction=True)


def test_old_tenant_statistics_before_growth_load(
    settings: SettingsWrapper,
    tmp_path: Path,
    django_assert_max_num_queries: DjangoAssertNumQueries,
) -> None:
    settings.MEDIA_ROOT = str(tmp_path / "old")
    call_command("seed_axorim_demo", "--firms=growth", "--seed=41", stdout=StringIO())
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
        try:
            cursor.execute(
                "ALTER TABLE clients_clientcompany SET (autovacuum_enabled=false);"
                "ALTER TABLE clients_engagement SET (autovacuum_enabled=false);"
                "ALTER TABLE clients_taxprofile SET (autovacuum_enabled=false);"
                "ALTER TABLE obligations_obligation SET (autovacuum_enabled=false);"
                "ALTER TABLE cycles_servicecycle SET (autovacuum_enabled=false);"
                "ALTER TABLE cycles_evidencerequest SET (autovacuum_enabled=false);"
                "ALTER TABLE cycles_cycleevent SET (autovacuum_enabled=false);"
                "ALTER TABLE cycles_evidencelink SET (autovacuum_enabled=false);"
                "ALTER TABLE cycles_documentintake SET (autovacuum_enabled=false);"
                "ALTER TABLE delivery_outboundmessage SET (autovacuum_enabled=false)"
            )
            cursor.execute(
                "ANALYZE clients_clientcompany, clients_engagement, clients_taxprofile,"
                "obligations_obligation, cycles_servicecycle, cycles_evidencerequest,"
                "cycles_cycleevent, cycles_evidencelink, cycles_documentintake,"
                "delivery_outboundmessage"
            )
            cursor.execute("TRUNCATE clients_clientcompany CASCADE")
        finally:
            cursor.execute("SET ROLE app_runtime")
    assert_isolated_role()
    test_read_budgets.test_named_reads_on_seeded_growth_portfolio(
        settings, tmp_path / "new", django_assert_max_num_queries, 0
    )
