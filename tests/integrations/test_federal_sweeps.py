"""AXRM-018: PAGTOWEB checks are scheduled fairly, within every guard, and visible.

Beat fires `schedule_payment_consultations` daily. These tests drive that real task.
Fairness runs against 3 firms x 50 clients with the per-client action replaced by a
recorder; every guard and the heartbeat run against the loopback `FakeSerpro`,
counting the requests that actually reach it. Missing implementation is an
assertion, never an import error. The SITFIS sweep is in
`test_federal_sweeps_sitfis.py` (its fixtures cannot share a name with these).
"""

import importlib
import importlib.util
from collections.abc import Callable
from datetime import date
from types import ModuleType, SimpleNamespace
from uuid import UUID

import pytest
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.briefing import providers
from apps.briefing.source_rows import SourceContext
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.cycles.models import Exception as CycleException
from apps.delivery.jobs import registered_jobs
from apps.integrations.authority import head_record, revoke_authority
from apps.integrations.budget_models import ProviderBudget
from apps.integrations.models import (
    ClientProviderLink,
    IntegrationIssue,
    ProviderConnection,
)
from apps.integrations.serpro import tasks
from apps.integrations.serpro.models import PaymentQuery
from apps.obligations.models import SchedulerHeartbeat
from apps.tenants.models import Tenant
from tests.integrations import pagtoweb_support as pagto
from tests.integrations.pagtoweb_support import World, page, world

__all__ = ["world"]

pytestmark = pytest.mark.django_db(transaction=True)

DAY = date(2026, 10, 21)
PAYMENTS = "schedule_payment_consultations"

Fire = Callable[[date], int]


def sweeps() -> ModuleType:
    name = "apps.integrations.serpro.sweeps"
    assert importlib.util.find_spec(name) is not None, f"AXRM-018 must supply {name}"
    return importlib.import_module(name)


def on(monkeypatch: pytest.MonkeyPatch, day: date) -> None:
    monkeypatch.setattr(tasks, "timezone", SimpleNamespace(localdate=lambda: day))


def cnpj(base: str) -> str:
    """A valid alphanumeric CNPJ from its 12-character root and order."""
    digits = base
    for weights in (
        (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2),
        (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2),
    ):
        total = sum((ord(c) - 48) * w for c, w in zip(digits, weights, strict=True))
        digits += str(0 if total % 11 < 2 else 11 - total % 11)
    return digits


@pytest.fixture
def fire(world: World, monkeypatch: pytest.MonkeyPatch) -> Fire:
    """Run the beat entry on `day`; the run tasks it enqueues execute in-process."""

    def run_now(tenant_id: str, query_id: str) -> object:
        return tasks.run_payment_query_task.apply(args=(tenant_id, query_id)).get()

    monkeypatch.setattr(tasks.run_payment_query_task, "delay", run_now)

    def fire_on(day: date) -> int:
        on(monkeypatch, day)
        queued: int = getattr(tasks, PAYMENTS).apply().get()
        return queued

    return fire_on


def stamp() -> SchedulerHeartbeat | None:
    return SchedulerHeartbeat.objects.filter(name=PAYMENTS).first()


def states(world: World) -> list[str]:
    with tenant_context(world.termo.tenant.pk):
        return list(
            PaymentQuery.objects.order_by("created_at").values_list("state", flat=True)
        )


def paid(world: World) -> int:
    return len(world.sent())


def authorize(world: World, client: ClientCompany) -> None:
    """Declare and verify 00004 for another client of the same firm."""
    from apps.integrations.authority import (  # noqa: PLC0415
        declare_authority,
        head_record,
        verify_authority,
    )

    termo = world.termo
    with tenant_context(termo.tenant.pk):
        head = head_record(client.pk, "pagtoweb")
        declare_authority(
            termo.actor,
            client.pk,
            service="pagtoweb",
            code="00004",
            valid_from=date(2026, 1, 1),
            expected_version=head.version if head else 0,
        )
        head = head_record(client.pk, "pagtoweb")
        assert head is not None
        verify_authority(
            termo.actor, client.pk, service="pagtoweb", expected_version=head.version
        )


def add_clients(world: World, count: int, *, mei: bool) -> list[ClientCompany]:
    """More linked, authorized clients in the world's firm."""
    made = []
    start = 10 if mei else 50
    for n in range(count):
        with tenant_context(world.termo.tenant.pk):
            client = ClientCompany.objects.create(
                tenant=world.termo.tenant,
                legal_name=f"Extra {mei}-{n}",
                cnpj=cnpj(f"AB{start + n:04d}000001"),
                is_mei=mei,
            )
            ClientProviderLink.objects.create(
                tenant=world.termo.tenant,
                client=client,
                connection_id=world.termo.connection.pk,
                provider_company_ref=f"extra-{mei}-{n}",
                status="ativa",
            )
        authorize(world, client)
        made.append(client)
    return made


# ---- fairness ---------------------------------------------------------------


def firms(count: int, clients: int) -> dict[UUID, list[UUID]]:
    """`count` flagged firms, each with `clients` linked clients."""
    made: dict[UUID, list[UUID]] = {}
    for n in range(count):
        tenant = Tenant.objects.create(
            name=f"Fair {n}",
            slug=f"axrm018-fair-{n}-{UUID(int=n).hex[-6:]}",
            features=["cycles_r1", "scan_required", "federal_checks"],
        )
        with tenant_context(tenant.pk):
            connection = ProviderConnection.objects.create(
                tenant=tenant, provider="serpro", environment="fake", status="ativa"
            )
            ids = []
            for i in range(clients):
                client = ClientCompany.objects.create(
                    tenant=tenant,
                    legal_name=f"Cliente {n}-{i}",
                    cnpj=cnpj(f"FA{n:02d}{i:04d}0001"),
                    is_mei=True,
                )
                ClientProviderLink.objects.create(
                    tenant=tenant,
                    client=client,
                    connection=connection,
                    provider_company_ref=f"ref-{n}-{i}",
                    status="ativa",
                )
                ids.append(client.pk)
        made[tenant.pk] = sorted(ids)
    return made


@pytest.fixture
def lane(settings: SettingsWrapper) -> None:
    settings.PROVIDERS_PAUSED = False
    settings.SERPRO_PAUSED = False
    settings.SERPRO_ENVIRONMENT = "fake"
    settings.PROVIDER_REAL_DATA_APPROVED = False


def test_no_firm_or_client_is_starved_across_runs(
    lane: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given 3 firms x 50 linked clients and a recorder in place of the paid action.
    module = sweeps()
    made = firms(3, 50)
    visits: list[tuple[UUID, UUID]] = []

    def record(client: ClientCompany, window: object) -> object:
        visits.append((client.tenant_id, client.pk))
        return module.Outcome.QUEUED

    monkeypatch.setattr(module, "payments_for", record)
    on(monkeypatch, DAY)
    per_run: list[list[tuple[UUID, UUID]]] = []
    # When beat fires three times (50 clients need ceil(50/20) runs per firm).
    for _ in range(3):
        before = len(visits)
        getattr(tasks, PAYMENTS).apply().get()
        per_run.append(visits[before:])
    # Then every run visits every firm, never more than 20 clients of one firm,
    for run in per_run:
        for tenant_id in made:
            assert 0 < sum(1 for t, _ in run if t == tenant_id) <= 20
    # and after those runs every client of every firm was visited at least once,
    seen = {pair for pair in visits if pair[0] in made}
    assert seen == {(t, c) for t, ids in made.items() for c in ids}
    # and the third run wrapped around to the start rather than stalling.
    for tenant_id, ids in made.items():
        third = [c for t, c in per_run[2] if t == tenant_id]
        assert third == ids[40:] + ids[:10]


def test_the_cursor_survives_worker_death_without_skipping_or_repeating(
    lane: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given one firm whose worker dies on its 8th client of the first run.
    module = sweeps()
    [(tenant_id, ids)] = firms(1, 50).items()
    seen: list[UUID] = []
    dying: list[UUID] = [ids[7]]

    def record(client: ClientCompany, window: object) -> object:
        if dying and client.pk == dying[0]:
            dying.clear()
            raise SystemExit  # a killed worker: nothing after this line runs
        if client.tenant_id == tenant_id:
            seen.append(client.pk)
        return module.Outcome.QUEUED

    monkeypatch.setattr(module, "payments_for", record)
    on(monkeypatch, DAY)
    with pytest.raises(SystemExit):
        getattr(tasks, PAYMENTS).apply(throw=True)
    assert seen == ids[:7]
    # When the next run starts, it resumes at the client that was in flight.
    getattr(tasks, PAYMENTS).apply().get()
    assert seen[7:] == ids[7:27]
    assert len(seen) == len(set(seen))


# ---- guards: a refusal never reaches I/O ------------------------------------


@pytest.mark.parametrize("flag", ["PROVIDERS_PAUSED", "SERPRO_PAUSED"])
def test_a_pause_runs_nothing_and_shows_as_paused(
    world: World, fire: Fire, settings: SettingsWrapper, flag: str
) -> None:
    setattr(settings, flag, True)
    world.script(page([]))
    assert fire(DAY) == 0
    assert paid(world) == 0
    assert states(world) == []
    beat = stamp()
    assert beat is not None
    assert beat.last_success_at is None
    assert beat.last_error_code == "pausado"
    row = next(j for j in registered_jobs() if j.name == "federal-payments-monthly")
    assert row.last_error_code == "pausado"


def test_the_flag_off_runs_nothing(world: World, fire: Fire) -> None:
    Tenant.objects.filter(pk=world.termo.tenant.pk).update(features=["cycles_r1"])
    world.script(page([]))
    assert fire(DAY) == 0
    assert paid(world) == 0
    assert states(world) == []


def test_an_unapproved_production_environment_runs_nothing(
    world: World, fire: Fire, settings: SettingsWrapper
) -> None:
    settings.SERPRO_ENVIRONMENT = "producao"
    settings.PROVIDER_REAL_DATA_APPROVED = False
    world.script(page([]))
    assert fire(DAY) == 0
    assert paid(world) == 0
    assert states(world) == []
    beat = stamp()
    assert beat is not None
    assert beat.last_success_at is None


def test_an_exhausted_budget_refuses_before_any_request(
    world: World, fire: Fire
) -> None:
    with tenant_context(world.termo.tenant.pk):
        ProviderBudget.objects.update(cap_microbrl=0)
    world.script(page([]))
    fire(DAY)
    assert paid(world) == 0
    assert states(world) == ["cota_esgotada"]


def test_a_client_without_verified_authority_is_not_consulted_and_is_surfaced(
    world: World, fire: Fire
) -> None:
    with tenant_context(world.termo.tenant.pk):
        head = head_record(world.termo.client.pk, "pagtoweb")
        assert head is not None
        revoke_authority(
            world.termo.actor,
            world.termo.client.pk,
            service="pagtoweb",
            expected_version=head.version,
        )
    world.script(page([]))
    assert fire(DAY) == 0
    assert paid(world) == 0
    assert states(world) == []
    with tenant_context(world.termo.tenant.pk):
        surfaced = (
            IntegrationIssue.objects.filter(
                client_id=world.termo.client.pk, reason="autorizacao_ausente"
            ).exists()
            or CycleException.objects.filter(
                client_id=world.termo.client.pk, reason_code="autorizacao_ausente"
            ).exists()
        )
    assert surfaced


# ---- heartbeat, idempotency, happy path -------------------------------------


def test_success_stamps_the_heartbeat_and_a_second_run_that_day_pays_nothing(
    world: World, fire: Fire
) -> None:
    world.script(page([pagto.item("DAS000000000010")]))
    assert fire(DAY) == 1
    assert paid(world) == 1
    beat = stamp()
    assert beat is not None
    assert beat.last_success_at is not None
    assert beat.last_error_code == ""
    assert fire(DAY) == 0
    assert fire(date(2026, 10, 22)) == 0
    assert paid(world) == 1


def test_a_failed_tenant_turn_never_stamps_success(
    lane: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = sweeps()
    firms(2, 3)

    def boom(client: ClientCompany, window: object) -> object:
        message = "provider down"
        raise RuntimeError(message)

    monkeypatch.setattr(module, "payments_for", boom)
    on(monkeypatch, DAY)
    getattr(tasks, PAYMENTS).apply()
    beat = stamp()
    assert beat is not None
    assert beat.last_success_at is None
    assert beat.last_error_code == "federal_sweep_failed"


def test_five_clients_with_authority_get_five_consultations(
    world: World, fire: Fire
) -> None:
    add_clients(world, 2, mei=True)
    add_clients(world, 2, mei=False)
    world.script(page([pagto.item("DAS000000000010")]), *[page([]) for _ in range(4)])
    assert fire(DAY) == 5
    assert paid(world) == 5
    assert len(states(world)) == 5
    beat = stamp()
    assert beat is not None
    assert beat.last_success_at is not None


# ---- Hoje ---------------------------------------------------------------------


def test_the_hoje_queue_lists_an_open_integration_issue(world: World) -> None:
    with tenant_context(world.termo.tenant.pk):
        IntegrationIssue.objects.create(
            tenant=world.termo.tenant,
            client=world.termo.client,
            reason="consulta_pagamentos_falhou",
            owner=world.termo.actor,
            review_on=DAY,
        )
        context = SourceContext(clients=ClientCompany.objects.all(), now=timezone.now())
        kinds = {item.kind for item in providers.source_items(context)}
    assert "integration" in kinds
