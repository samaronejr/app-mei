"""The two federal sweeps' automation rows: label, empty health, and pause scope."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from django.utils import timezone

from apps.delivery import jobs
from apps.delivery.jobs import JOB_LABELS, JobStatus, registered_jobs
from apps.obligations.models import SchedulerHeartbeat

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

pytestmark = pytest.mark.django_db(transaction=True)

SWEEPS = {
    "federal-payments-monthly": "schedule_payment_consultations",
    "federal-sitfis-monthly": "schedule_situation_reports",
}


def _jobs() -> tuple[JobStatus, ...]:
    """Turn any crash of the job rows into a failed assertion about them."""
    try:
        return registered_jobs()
    except Exception as error:
        message = f"the job rows must not raise: {error!r}"
        raise AssertionError(message) from error


def _row(name: str) -> JobStatus:
    return next(job for job in _jobs() if job.name == name)


def test_the_federal_sweeps_are_the_only_pause_aware_jobs() -> None:
    assert getattr(jobs, "PAUSE_AWARE", None) == set(SWEEPS.values())
    assert getattr(jobs, "PAUSED_CODE", None) == "pausado"


@pytest.mark.parametrize("name", sorted(SWEEPS))
def test_a_federal_sweep_row_has_its_own_label_and_no_health_before_a_run(
    name: str, settings: SettingsWrapper
) -> None:
    settings.PROVIDERS_PAUSED = False
    settings.SERPRO_PAUSED = False
    row = _row(name)
    short = SWEEPS[name]
    assert JOB_LABELS[short]
    assert row.label == JOB_LABELS[short]
    assert row.label != short.replace("_", " ")
    assert row.last_error_code == ""


@pytest.mark.parametrize(
    ("providers_paused", "serpro_paused"), [(True, False), (False, True), (True, True)]
)
def test_a_pause_leaves_every_other_job_row_with_its_own_health(
    settings: SettingsWrapper, providers_paused: bool, serpro_paused: bool
) -> None:
    SchedulerHeartbeat.objects.create(
        name="purge_payment_pages",
        last_attempt_at=timezone.now(),
        last_success_at=timezone.now(),
        last_error_code="purge_failed",
    )
    settings.PROVIDERS_PAUSED = providers_paused
    settings.SERPRO_PAUSED = serpro_paused
    others = [job for job in _jobs() if job.name not in set(SWEEPS)]
    assert others
    assert {job.last_error_code for job in others} <= {"", "purge_failed"}
    purge = next(job for job in others if job.name.endswith("purge-payment-pages"))
    assert purge.last_error_code == "purge_failed"
