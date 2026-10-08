"""AXR1-022 metering counts attempted provider dispatches, once per AgentRun."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from decimal import Decimal
from io import StringIO
from threading import Barrier
from uuid import UUID

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.db import connection, connections
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.automation import shadow
from apps.automation.admission import Ticket, claim
from apps.automation.dataset import minimal_pdf
from apps.automation.llm import FakeLLM
from apps.automation.models import AgentRun, EvalRun
from apps.automation.shadow import run_shadow
from apps.automation.types import Budget, JSONObject, Message, ProviderError, Result
from apps.core.tenancy import tenant_context
from apps.cycles.evidence import DocumentIntake
from apps.tenants.models import Tenant, UsageKind, UsageMetric, UsageRecord
from apps.tenants.usage import cost_estimate
from tests.ai.fixtures import single_company
from tests.cycles.document_fixtures import reviewed
from tests.portal.request_fixtures import PortalSetup, portal_setup
from tests.support_threads import run_in_thread_as_runtime

pytestmark = pytest.mark.django_db(transaction=True)

AI = UsageMetric.AI_SHADOW_CALLS
# 23:30 on 30 September in Sao Paulo is already 1 October in UTC.
LATE_SEPTEMBER = datetime(2026, 10, 1, 2, 30, tzinfo=UTC)
EARLY_OCTOBER = datetime(2026, 10, 1, 3, 30, tzinfo=UTC)
OCTOBER = date(2026, 10, 1)


def source(setup: PortalSetup, number: int) -> DocumentIntake:
    payload = minimal_pdf(
        f"Recibo {number}. CNPJ: 11222333000181. Competencia: 09/2026"
    )
    intake, _ = reviewed(setup, f"recibo_{number}.pdf", payload)
    return intake


def at(monkeypatch: pytest.MonkeyPatch, instant: datetime) -> None:
    monkeypatch.setattr(timezone, "now", lambda: instant)


def calls(tenant_id: UUID) -> list[UsageRecord]:
    with tenant_context(tenant_id):
        return list(
            UsageRecord.objects.filter(metric=AI).order_by("period", "created_at")
        )


def committed_calls() -> int:
    return UsageRecord.objects.filter(metric=AI).count()


def meter_concurrently(tenant_id: UUID, run_id: UUID) -> list[bool]:
    ready = Barrier(2, timeout=15)

    def meter() -> bool:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET ROLE app_runtime")
            ready.wait()
            return shadow.record_dispatch(tenant_id, run_id)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(meter) for _ in range(2)]
        return sorted(future.result(timeout=30) for future in futures)


@pytest.mark.parametrize(
    ("instant", "period"),
    [(LATE_SEPTEMBER, date(2026, 9, 30)), (EARLY_OCTOBER, OCTOBER)],
    ids=["sao-paulo-day-before-utc", "same-day"],
)
def test_a_dispatched_run_is_metered_once_and_committed_before_the_call(
    setup: PortalSetup, monkeypatch: pytest.MonkeyPatch, instant: datetime, period: date
) -> None:
    intake = single_company(setup)
    tid = setup.graph.tenant.pk
    at(monkeypatch, instant)
    seen_by_another_session: list[int] = []
    original = FakeLLM.complete

    def observe(
        self: FakeLLM,
        schema: JSONObject,
        messages: tuple[Message, ...],
        budget: Budget,
    ) -> Result:
        seen_by_another_session.append(run_in_thread_as_runtime(tid, committed_calls))
        return original(self, schema, messages, budget)

    monkeypatch.setattr(FakeLLM, "complete", observe)
    assert run_shadow(tid, intake.pk) == "completed"
    assert seen_by_another_session == [1]
    with tenant_context(tid):
        run = AgentRun.objects.get()
    [record] = calls(tid)
    assert record.tenant_id == tid
    assert record.source_event == run.pk
    assert (record.quantity, record.kind, record.period) == (
        1,
        UsageKind.CONFIRMED,
        period,
    )
    assert record.correction_of_id is None


def test_redelivery_and_a_repeated_meter_step_never_count_twice(
    setup: PortalSetup,
) -> None:
    first, second = source(setup, 1), source(setup, 2)
    tid = setup.graph.tenant.pk
    assert run_shadow(tid, first.pk) == "completed"
    assert run_shadow(tid, first.pk) == "skipped"
    with tenant_context(tid):
        run = AgentRun.objects.get()
    assert shadow.record_dispatch(tid, run.pk) is False
    # A redelivered worker re-entering the meter step for a run it already claimed.
    ticket = claim(tid, second.pk, now=timezone.now())
    assert ticket is not None
    assert meter_concurrently(tid, ticket.run_id) == [False, True]
    assert sorted(str(record.source_event) for record in calls(tid)) == sorted(
        [str(run.pk), str(ticket.run_id)]
    )


@pytest.mark.parametrize(
    "cause", ["local_abstention", "changed_source", "missing_source", "gate_closed"]
)
def test_runs_that_end_before_dispatch_are_not_metered(
    setup: PortalSetup, monkeypatch: pytest.MonkeyPatch, cause: str
) -> None:
    tid = setup.graph.tenant.pk
    if cause == "local_abstention":
        # The portal fixture names the client and a counterpart: two companies.
        intake, _ = reviewed(setup, "recibo_cliente.pdf")
    else:
        intake = single_company(setup)
    if cause == "changed_source":
        with default_storage.open(intake.document.storage_key, "wb") as stream:
            stream.write(b"changed since deterministic extraction")
    if cause == "missing_source":
        default_storage.delete(intake.document.storage_key)
    if cause == "gate_closed":
        claimed = claim

        def claim_then_close(
            tenant_id: UUID, intake_id: UUID, *, now: datetime
        ) -> Ticket | None:
            ticket = claimed(tenant_id, intake_id, now=now)
            Tenant.objects.filter(pk=tid).update(features=["cycles_r1"])
            return ticket

        monkeypatch.setattr(shadow, "claim", claim_then_close)

    def unexpected(*args: object) -> None:
        pytest.fail("provider called although a local decision ended the run")

    monkeypatch.setattr(FakeLLM, "complete", unexpected)
    expected = {
        "local_abstention": ("completed", ""),
        "changed_source": ("provider_error", "source_integrity"),
        "missing_source": ("provider_error", "source_unreadable"),
        "gate_closed": ("provider_error", "admission_changed"),
    }[cause]
    assert run_shadow(tid, intake.pk) == expected[0]
    with tenant_context(tid):
        run = AgentRun.objects.get()
    assert (run.state, run.error_code) == expected
    if cause == "local_abstention":
        assert run.output["confidence_band"] == "abstain"
    assert calls(tid) == []


def _provider_error(*args: object) -> Result:
    message = "unavailable"
    raise ProviderError(message)


def _timeout(*args: object) -> Result:
    raise SoftTimeLimitExceeded


def _other_client(*args: object) -> Result:
    return Result(
        json.dumps(
            {
                "doc_type": "receipt",
                "fields": [],
                "client_cnpj_seen": "11444777000161",
                "confidence_band": "high",
            }
        )
    )


@pytest.mark.parametrize(
    ("reply", "state"),
    [
        (_provider_error, "provider_error"),
        (_timeout, "provider_error"),
        (_other_client, "rejected"),
    ],
    ids=["provider-error", "timeout", "rejected-output"],
)
def test_a_failure_after_dispatch_is_still_metered(
    setup: PortalSetup, monkeypatch: pytest.MonkeyPatch, reply: object, state: str
) -> None:
    intake = single_company(setup)
    tid = setup.graph.tenant.pk
    monkeypatch.setattr(FakeLLM, "complete", reply)
    assert run_shadow(tid, intake.pk) == state
    with tenant_context(tid):
        run = AgentRun.objects.get()
    assert [(record.source_event, record.quantity) for record in calls(tid)] == [
        (run.pk, 1)
    ]


@pytest.mark.parametrize("gate", ["flag", "kill_switch", "provider", "refused"])
def test_closed_gates_and_refused_admission_meter_nothing(
    setup: PortalSetup, settings: SettingsWrapper, gate: str
) -> None:
    intake = single_company(setup)
    tid = setup.graph.tenant.pk
    if gate == "flag":
        Tenant.objects.filter(pk=tid).update(features=["cycles_r1"])
    if gate == "kill_switch":
        settings.AI_GLOBAL_DISABLED = True
    if gate == "provider":
        settings.AI_PROVIDER = ""
    if gate == "refused":
        settings.AI_SHADOW_TENANT_DAILY_CALLS = 0
    assert run_shadow(tid, intake.pk) == "skipped"
    with tenant_context(tid):
        assert not AgentRun.objects.exists()
    assert calls(tid) == []


def test_synthetic_evaluation_is_never_metered(setup: PortalSetup) -> None:
    call_command("run_eval", "--model", "fake", "--set", "v1", stdout=StringIO())
    assert EvalRun.objects.count() == 1
    tenants = list(Tenant.objects.values_list("pk", flat=True))
    assert setup.graph.tenant.pk in tenants
    for tid in tenants:
        assert calls(tid) == []


def test_records_are_private_to_their_tenant_and_counted_per_tenant(
    setup: PortalSetup,
) -> None:
    other = portal_setup("t24-usage-other")
    Tenant.objects.filter(pk=other.graph.tenant.pk).update(
        features=["cycles_r1", "ai_shadow"]
    )
    firms = (setup, other)
    intakes = [single_company(firm) for firm in firms]
    for firm, intake in zip(firms, intakes, strict=True):
        assert run_shadow(firm.graph.tenant.pk, intake.pk) == "completed"
    records = {firm.graph.tenant.pk: calls(firm.graph.tenant.pk) for firm in firms}
    for tid, [record] in records.items():
        assert record.tenant_id == tid
        with tenant_context(tid):
            assert AgentRun.objects.get().pk == record.source_event
    first, second = (records[firm.graph.tenant.pk][0] for firm in firms)
    assert first.period == second.period == timezone.localdate()
    assert first.source_event != second.source_event
    with tenant_context(setup.graph.tenant.pk):
        assert not UsageRecord.objects.filter(pk=second.pk).exists()
    stranger = Tenant.objects.create(name="Another firm", slug="t24-usage-stranger")
    with tenant_context(stranger.pk), connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM tenants_usagerecord")
        assert cursor.fetchone() == (0,)


def test_owner_estimate_sums_this_months_calls_and_needs_a_price(
    setup: PortalSetup, settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch
) -> None:
    september, *october = (source(setup, number) for number in (1, 2, 3))
    tid = setup.graph.tenant.pk
    at(monkeypatch, LATE_SEPTEMBER)
    assert run_shadow(tid, september.pk) == "completed"
    at(monkeypatch, EARLY_OCTOBER)
    for intake in october:
        assert run_shadow(tid, intake.pk) == "completed"
    assert [(record.period, record.quantity) for record in calls(tid)] == [
        (date(2026, 9, 30), 1),
        (OCTOBER, 1),
        (OCTOBER, 1),
    ]
    settings.USAGE_UNIT_PRICES_BRL = {}
    with tenant_context(tid):
        unpriced = cost_estimate(setup.graph.actor, tid, on=OCTOBER)
    [line] = (line for line in unpriced.lines if line.metric == AI)
    assert line.quantity == len(october)
    assert line.cost is None
    assert not unpriced.complete
    assert unpriced.total is None
    settings.USAGE_UNIT_PRICES_BRL = {AI: "0.03"}
    with tenant_context(tid):
        priced = cost_estimate(setup.graph.actor, tid, on=OCTOBER)
    [line] = (line for line in priced.lines if line.metric == AI)
    assert line.cost == Decimal("0.06")
    # Gauges were never measured and the other metrics have no price.
    assert not priced.complete
    assert priced.total is None
