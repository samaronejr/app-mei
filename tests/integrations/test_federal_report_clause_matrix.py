"""Correlated cycles and reports must not borrow another client's month."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import pytest

from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.cycles.models import ServiceCycle
from apps.integrations.models import ClientProviderLink
from apps.integrations.serpro import sweeps
from apps.integrations.serpro.sitfis import request_situation_report
from apps.obligations.models import FiscalSituationReport
from tests.integrations.sitfis_support import (
    World,
    _declare_sitfis,
    _open_cycle,
    world,
)
from tests.integrations.test_federal_sweeps import cnpj
from tests.integrations.test_federal_sweeps_sitfis import clock

__all__ = ["clock", "world"]
if TYPE_CHECKING:
    from uuid import UUID

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    "clause",
    [
        "cycle-client",
        "cycle-month",
        "cycle-state",
        "open-cycle",
        "report-client",
        "report-month",
        "report-state",
        "received-report",
    ],
)
def test_a_report_slot_belongs_to_the_clients_open_month(
    world: World,
    clause: str,
) -> None:
    with tenant_context(world.tenant_id):
        other = ClientCompany.objects.create(
            tenant_id=world.tenant_id,
            legal_name=f"Clause {clause}",
            cnpj=cnpj("RC0001000001"),
            is_mei=True,
        )
        ClientProviderLink.objects.create(
            tenant_id=world.tenant_id,
            client=other,
            connection=world.termo.connection,
            provider_company_ref="report-clause",
            status="ativa",
        )
    termo = replace(world.termo, client=other)
    _declare_sitfis(termo)
    has_cycle = clause not in {"cycle-client", "open-cycle"}
    if has_cycle:
        cycle = _open_cycle(termo, world.responsible)
        with tenant_context(world.tenant_id):
            if clause == "cycle-month":
                ServiceCycle.objects.filter(pk=cycle.pk).update(
                    period_start=date(2026, 9, 1),
                    period_end=date(2026, 9, 30),
                )
            if clause == "cycle-state":
                ServiceCycle.objects.filter(pk=cycle.pk).update(state="concluido")
    with tenant_context(world.tenant_id):
        if clause.startswith("report-") or clause == "received-report":
            report = request_situation_report(other)
            changes: dict[str, object] = {
                "state": "solicitado"
                if clause == "report-state"
                else "protocolo_recebido",
            }
            if clause == "report-month":
                changes["requested_at"] = datetime(2026, 9, 30, 15, tzinfo=UTC)
            FiscalSituationReport.objects.filter(pk=report.pk).update(**changes)
        expected = {world.termo.client.pk}
        if clause in {"report-month", "report-state"}:
            expected.add(other.pk)
        actual = set(
            sweeps.report_candidates(date(2026, 10, 21)).values_list("pk", flat=True)
        )
        assert actual == expected
    visited: list[UUID] = []

    def record(client: ClientCompany) -> sweeps.Outcome:
        visited.append(client.pk)
        return sweeps.Outcome.QUEUED

    for day in range(21, 32):
        today = date(2026, 10, day)
        queued, failed = sweeps._turn(
            world.tenant_id,
            "federal_sitfis_cursor",
            lambda current=today: sweeps.report_candidates(current),
            record,
        )
        assert (queued, failed) == (len(expected), 0)
        assert set(visited) <= expected
    assert set(visited) == expected
