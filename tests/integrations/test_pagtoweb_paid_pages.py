"""AXRM-015 review 5: a paid PAGTOWEB page is kept until its consultation settles it.

The page call's success stores the parsed page (`PaymentPage`) in its own
finalize transaction. These tests pin what is kept, when it goes (applied,
consultation ended, daily retention), and that another firm can neither read nor
attach one. Every runner is a real `run_payment_query` against the loopback
`FakeSerpro`; nothing in the protocol, parser, RLS or storage is replaced.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from django.conf import settings
from django.db import DatabaseError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.integrations.call_models import CallState
from apps.integrations.serpro import payments as payment_service
from apps.integrations.serpro import tasks
from apps.integrations.serpro.models import PaymentPage, PaymentQuery
from apps.integrations.serpro.payments import open_payment_query
from apps.integrations.serpro.qualification import synthetic_termo
from tests.integrations.pagtoweb_support import WINDOW, World, darfs, item, page, world
from tests.integrations.test_migration_rollback import _as_migrator
from tests.integrations.test_pagtoweb import runner, scheduled_query, settled
from tests.integrations.test_pagtoweb_ownership import (
    Stop,
    WorkerDiedError,
    dies_right_after_paying,
    lease_runs_out,
    redeliver,
    run,
    stop_at,
)

__all__ = ["world"]

pytestmark = pytest.mark.django_db(transaction=True)

KEPT_FIELDS = {
    "numero_documento",
    "tipo",
    "periodo_apuracao",
    "data_arrecadacao",
    "data_vencimento",
    "receita_codigo",
    "receita_descricao",
    "valor_total",
    "valor_principal",
    "valor_multa",
    "valor_juros",
}


def paid_and_orphaned(world: World, monkeypatch: pytest.MonkeyPatch) -> PaymentQuery:
    """A paid for page 1 of a scheduled consultation and died at once."""
    query = scheduled_query(world)
    dies_right_after_paying(monkeypatch, "owner")
    world.script(page([item("DAS-KEPT-01", total=76.6, principal=None)]))
    pool, first = runner("owner", lambda: run(query))
    with pytest.raises(WorkerDiedError):
        first.result(timeout=25)
    pool.shutdown(wait=True)
    return query


def kept(world: World) -> list[PaymentPage]:
    with tenant_context(world.termo.tenant.pk):
        return list(PaymentPage.objects.order_by("created_at"))


def test_the_paid_page_is_kept_with_its_success_and_only_its_payment_fields(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given A died the moment its page call was finalized as paid.
    query = paid_and_orphaned(world, monkeypatch)
    # Then the page was committed with that success: same call, same page.
    [call] = world.calls()
    assert (call.state, call.billed) == (CallState.SUCCESS, "sim")
    [stored] = kept(world)
    assert (stored.query_id, stored.index, stored.call_id) == (query.pk, 0, call.pk)
    assert (stored.tenant_id, stored.client_id) == (query.tenant_id, query.client_id)
    # And it holds the normalized payment fields alone, never provider text.
    [row] = stored.records
    assert set(row) == KEPT_FIELDS
    assert row["numero_documento"] == "DAS-KEPT-01"
    assert (row["tipo"], row["periodo_apuracao"]) == ("DAS", "2026-09-01")
    assert Decimal(str(row["valor_total"])) == Decimal("76.6")
    assert row["valor_principal"] is None
    # When it is adopted, it is applied once and no longer kept.
    lease_runs_out(world, query.pk)
    assert redeliver(query) == "encontrado"
    assert kept(world) == []
    view = settled(world, query.pk)
    assert (view["payments"], view["requests"]) == (1, 1)


def test_an_ordinary_consultation_keeps_no_page_once_settled(world: World) -> None:
    world.script(page([item("DAS-KEPT-02")]))
    query = world.consult()
    assert query.state == "encontrado"
    assert kept(world) == []


def test_an_applied_page_is_no_longer_kept_while_paging_goes_on(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given A applied a full page 1 and waits before reserving page 2.
    query = scheduled_query(world)
    between = Stop(at=2)
    stop_at(monkeypatch, "reserve", owner=between)
    world.script(page(darfs(100)), page(darfs(1, start=100)))
    pool, first = runner("owner", lambda: run(query))
    try:
        assert between.entered.wait(timeout=10)
        # Then page 1, applied, is no longer kept.
        assert kept(world) == []
    finally:
        between.release.set()
        pool.shutdown(wait=True)
    assert first.result(timeout=25) == "encontrado"
    assert kept(world) == []


def test_a_consultation_ending_without_its_kept_page_deletes_it(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a kept page no runner applied yet.
    query = paid_and_orphaned(world, monkeypatch)
    assert len(kept(world)) == 1
    # When the consultation ends some other way.
    lease_runs_out(world, query.pk)
    with payment_service._as_runner(query.tenant_id, uuid4()):
        payment_service._finish(
            payment_service._locked(query.pk), "falha", "encerrada_sem_pagina"
        )
    # Then its kept page goes with it.
    assert kept(world) == []


def test_the_daily_sweep_deletes_only_pages_nothing_will_settle(
    world: World,
) -> None:
    # Given a finished consultation with its paid call, a page left behind on
    # it, and an active consultation with a fresh kept page.
    world.script(page([item("DAS-KEPT-03")]))
    finished = world.consult()
    [call] = world.calls()
    tenant_id = world.termo.tenant.pk

    def leave(query: PaymentQuery) -> PaymentPage:
        with tenant_context(tenant_id):
            return PaymentPage.objects.create(
                tenant_id=tenant_id,
                client_id=query.client_id,
                query=query,
                call=call,
                index=0,
                records=[],
            )

    leave(finished)
    # When the beat-scheduled sweep runs, the finished consultation's page goes.
    assert tasks.purge_payment_pages.apply().get() == 1
    assert kept(world) == []
    with tenant_context(tenant_id):
        active = open_payment_query(
            world.termo.client, WINDOW, actor=world.termo.actor, nonce="sweep"
        )
    fresh = leave(active)
    # Then a fresh page of an active consultation stays.
    assert tasks.purge_payment_pages.apply().get() == 0
    assert [page.pk for page in kept(world)] == [fresh.pk]
    # And once it is older than the retention period, it goes too.
    with tenant_context(tenant_id):
        PaymentPage.objects.filter(pk=fresh.pk).update(
            created_at=timezone.now()
            - payment_service.PAGE_RETENTION
            - timedelta(minutes=1)
        )
    assert tasks.purge_payment_pages.apply().get() == 1
    assert kept(world) == []
    assert any(
        entry["task"] == "apps.integrations.serpro.tasks.purge_payment_pages"
        for entry in settings.CELERY_BEAT_SCHEDULE.values()
    )


def test_another_firm_can_neither_read_nor_attach_a_kept_page(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a kept page of this firm, and another firm with its own client.
    query = paid_and_orphaned(world, monkeypatch)
    [stored] = kept(world)
    other = synthetic_termo("fake")

    def as_other_firm() -> str:
        with tenant_context(other.tenant.pk):
            visible = PaymentPage.objects.count()
            by_id = PaymentPage.objects.filter(pk=stored.pk).exists()
        refusals = 0
        for tenant_id, client_id in (
            # Its own tenant and client, pointing at this firm's consultation.
            (other.tenant.pk, other.client.pk),
            # This firm's tenant and client, from the other firm's context.
            (query.tenant_id, query.client_id),
        ):
            try:
                with tenant_context(other.tenant.pk), transaction.atomic():
                    PaymentPage.objects.create(
                        tenant_id=tenant_id,
                        client_id=client_id,
                        query_id=query.pk,
                        call_id=stored.call_id,
                        index=1,
                        records=[],
                    )
            except DatabaseError:
                refusals += 1
        return f"{visible}:{by_id}:{refusals}"

    # When the other firm's runtime connection looks for it and tries to
    # attach a page to this firm's consultation.
    pool, result = runner("other", as_other_firm)
    try:
        # Then it sees nothing and every insert is refused.
        assert result.result(timeout=25) == "0:False:2"
    finally:
        pool.shutdown(wait=True)
    assert [page.pk for page in kept(world)] == [stored.pk]


def test_the_kept_page_table_reverses_populated_and_comes_back_isolated(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a kept page on file.
    query = paid_and_orphaned(world, monkeypatch)
    assert len(kept(world)) == 1
    with _as_migrator(), connection.cursor() as cursor:
        # When integrations.0010 is reversed with that row present.
        MigrationExecutor(connection).migrate(
            [("integrations", "0009_payment_query_owner_session")]
        )
        # Then only the table goes; the consultation stays as it was.
        cursor.execute("SELECT to_regclass('public.integrations_paymentpage')")
        assert cursor.fetchone() == (None,)
        cursor.execute(
            "SELECT state FROM integrations_paymentquery WHERE id = %s", [query.pk]
        )
        assert cursor.fetchone() == ("em_andamento",)
    # And re-applied (on leaving the migrator), it is empty, forced under RLS and
    # closed to the portal.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname = 'integrations_paymentpage'"
        )
        assert cursor.fetchone() == (True, True)
        cursor.execute(
            "SELECT has_table_privilege('app_portal', "
            "'integrations_paymentpage', 'SELECT')"
        )
        assert cursor.fetchone() == (False,)
    assert kept(world) == []
