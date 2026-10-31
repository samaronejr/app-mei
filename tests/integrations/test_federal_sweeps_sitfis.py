"""AXRM-018: the monthly SITFIS sweep requests one report per client and month.

The sweep only opens the report through todo 16's `request_situation_report` and
hands it to the phased emission task; these tests stub that task's `delay` and count
what was queued, so no provider byte is involved in the scheduling decision.
"""

import importlib
import importlib.util
from datetime import date
from types import SimpleNamespace

import pytest
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.delivery.jobs import registered_jobs
from apps.integrations.authority import head_record, revoke_authority
from apps.integrations.serpro import sitfis_tasks, tasks
from apps.obligations.models import FiscalSituationReport, SchedulerHeartbeat
from apps.tenants.models import Tenant
from tests.integrations.sitfis_support import Clock, World, clock, world

__all__ = ["clock", "world"]

pytestmark = pytest.mark.django_db(transaction=True)

DAY = date(2026, 10, 15)
SITFIS = "schedule_situation_reports"


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Capture the emission-task deliveries instead of running them."""
    delivered: list[tuple[str, str]] = []
    monkeypatch.setattr(
        sitfis_tasks.advance_situation_report_task,
        "delay",
        lambda tenant_id, report_id: delivered.append((tenant_id, report_id)),
    )
    return delivered


def fire(monkeypatch: pytest.MonkeyPatch, day: date = DAY) -> int:
    monkeypatch.setattr(tasks, "timezone", SimpleNamespace(localdate=lambda: day))
    assert hasattr(tasks, SITFIS), "AXRM-018 must supply the SITFIS sweep task"
    requested: int = getattr(tasks, SITFIS).apply().get()
    return requested


def reports(world: World) -> int:
    with tenant_context(world.tenant_id):
        return FiscalSituationReport.objects.count()


def test_a_client_whose_cycle_opened_this_month_gets_one_report_and_no_second(
    world: World, queued: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert fire(monkeypatch) == 1
    assert reports(world) == 1
    assert len(queued) == 1
    beat = SchedulerHeartbeat.objects.get(name=SITFIS)
    assert beat.last_success_at is not None
    assert beat.last_error_code == ""
    # Later days of the month find the report and request nothing new.
    assert fire(monkeypatch) == 0
    assert fire(monkeypatch, date(2026, 10, 16)) == 0
    assert reports(world) == 1
    assert len(queued) == 1


@pytest.mark.parametrize("flag", ["PROVIDERS_PAUSED", "SERPRO_PAUSED"])
def test_a_pause_requests_nothing_and_shows_as_paused(
    world: World,
    queued: list[tuple[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    settings: SettingsWrapper,
    flag: str,
) -> None:
    setattr(settings, flag, True)
    assert fire(monkeypatch) == 0
    assert reports(world) == 0
    assert queued == []
    beat = SchedulerHeartbeat.objects.get(name=SITFIS)
    assert beat.last_success_at is None
    assert beat.last_error_code == "pausado"
    row = next(j for j in registered_jobs() if j.name == "federal-sitfis-monthly")
    assert row.last_error_code == "pausado"


def test_the_flag_off_requests_nothing(
    world: World, queued: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    Tenant.objects.filter(pk=world.tenant_id).update(features=["cycles_r1"])
    assert fire(monkeypatch) == 0
    assert reports(world) == 0
    assert queued == []


def test_a_client_without_an_open_cycle_this_month_is_not_requested(
    world: World, queued: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The only cycle is October's; on a November day nothing has opened yet.
    assert fire(monkeypatch, date(2026, 11, 3)) == 0
    assert reports(world) == 0
    assert queued == []


def test_a_client_without_verified_authority_is_not_requested(
    world: World, queued: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    with tenant_context(world.tenant_id):
        head = head_record(world.termo.client.pk, "sitfis")
        assert head is not None
        revoke_authority(
            world.termo.actor,
            world.termo.client.pk,
            service="sitfis",
            expected_version=head.version,
        )
    assert fire(monkeypatch) == 0
    assert reports(world) == 0
    assert queued == []


def test_a_failed_turn_never_stamps_success(
    world: World,
    queued: list[tuple[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    clock: Clock,
) -> None:
    name = "apps.integrations.serpro.sweeps"
    assert importlib.util.find_spec(name) is not None, f"AXRM-018 must supply {name}"
    sweeps = importlib.import_module(name)

    def boom(client: object) -> object:
        message = "provider down"
        raise RuntimeError(message)

    monkeypatch.setattr(sweeps, "report_for", boom)
    monkeypatch.setattr(tasks, "timezone", SimpleNamespace(localdate=lambda: DAY))
    getattr(tasks, SITFIS).apply()
    beat = SchedulerHeartbeat.objects.get(name=SITFIS)
    assert beat.last_success_at is None
    assert beat.last_error_code == "federal_sweep_failed"
    assert queued == []
    assert clock.current is not None
