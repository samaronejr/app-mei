"""`cycles_federalreportreview` is append-only for every role and refuses loss.

AXRM-016 (A7.1, A11): trigger plus enumerated grants, and a reverse migration
that refuses while any review exists (forward repair only).
"""

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from django.db import DatabaseError, connection, transaction
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor

from apps.core.tenancy import tenant_context
from apps.obligations.models import FiscalSituationReport
from tests.integrations.sitfis_support import (
    World,
    clock,
    module,
    protocol_reply,
    report_reply,
    world,
)

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["clock", "world"]

TABLE = "cycles_federalreportreview"
BEFORE = ("cycles", "0021_pgmei_guide_choices")


@contextmanager
def _as_migrator() -> Iterator[None]:
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
    try:
        yield
    finally:
        MigrationExecutor(connection).migrate(leaves)
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE app_runtime")


def _reviewed(world: World) -> object:
    reviews = module("apps.cycles.services.federal_review")
    world.script(protocol_reply(30_000))
    report = world.advance(world.request())
    world.clock.advance(seconds=30)
    world.script(report_reply())
    world.advance(report)
    world.scan()
    with tenant_context(world.tenant_id):
        current = FiscalSituationReport.objects.get(pk=report.pk)
        from apps.cycles.models import ServiceCycle  # noqa: PLC0415

        version = ServiceCycle.objects.get(pk=world.cycle.pk).version
        return reviews.record_federal_report_review(
            world.responsible,
            world.cycle.pk,
            current.pk,
            outcome="sem_pendencias_nas_fontes",
            pendencias=[],
            expected_version=version,
            input_digest=reviews.report_input_digest(current),
        )


def _table_exists() -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass(%s) IS NOT NULL", [TABLE])
        row = cursor.fetchone()
    return bool(row and row[0])


def test_reviews_are_append_only_for_every_role_including_the_owner(
    world: World,
) -> None:
    review = _reviewed(world)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_trigger WHERE tgname = %s AND NOT tgisinternal",
            [f"{TABLE}_append_only"],
        )
        assert cursor.fetchone() == (1,)
        cursor.execute(
            "SELECT p, has_table_privilege('app_runtime', %s, p) "
            "FROM unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE']) p",
            [TABLE],
        )
        assert dict(cursor.fetchall()) == {
            "SELECT": True,
            "INSERT": True,
            "UPDATE": False,
            "DELETE": False,
            "TRUNCATE": False,
        }
        cursor.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname = %s",
            [TABLE],
        )
        assert cursor.fetchone() == (True, True)
        cursor.execute(
            "SELECT has_table_privilege('app_portal', %s, 'SELECT')", [TABLE]
        )
        assert cursor.fetchone() == (False,)
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
    try:
        for statement in (
            f"UPDATE {TABLE} SET outcome = 'ilegivel' WHERE id = %s",  # noqa: S608
            f"DELETE FROM {TABLE} WHERE id = %s",  # noqa: S608
        ):
            with (
                pytest.raises(DatabaseError, match="append-only"),
                transaction.atomic(),
                connection.cursor() as cursor,
            ):
                cursor.execute(statement, [review.pk])  # type: ignore[attr-defined]
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE app_runtime")


def test_the_empty_table_reverses_and_comes_back() -> None:
    with _as_migrator():
        MigrationExecutor(connection).migrate([BEFORE])
        assert not _table_exists()
    assert _table_exists()


def test_a_populated_table_refuses_to_reverse(world: World) -> None:
    review = _reviewed(world)
    with _as_migrator(), pytest.raises(IrreversibleError, match="forward repair"):
        MigrationExecutor(connection).migrate([BEFORE])
    from apps.cycles import models  # noqa: PLC0415

    with tenant_context(world.tenant_id):
        assert models.FederalReportReview.objects.filter(pk=review.pk).exists()  # type: ignore[attr-defined]
