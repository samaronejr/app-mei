"""The automation page must reflect live switches before another beat runs."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from django.utils import timezone

from apps.delivery.jobs import registered_jobs
from apps.obligations.models import SchedulerHeartbeat
from tests.delivery.test_boundaries import _web

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

    from tests.delivery.conftest import DeliverySetup

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    ("providers_paused", "serpro_paused"),
    [(False, False), (True, False), (False, True), (True, True)],
)
@pytest.mark.parametrize("previous", ["", "federal_sweep_failed", "pausado"])
def test_live_pause_overrides_prior_federal_health_before_the_next_sweep(
    delivery: DeliverySetup,
    settings: SettingsWrapper,
    providers_paused: bool,
    serpro_paused: bool,
    previous: str,
) -> None:
    names = ("schedule_payment_consultations", "schedule_situation_reports")
    for name in names:
        SchedulerHeartbeat.objects.create(
            name=name,
            last_attempt_at=timezone.now(),
            last_success_at=timezone.now(),
            last_error_code=previous,
        )
    settings.PROVIDERS_PAUSED = providers_paused
    settings.SERPRO_PAUSED = serpro_paused
    expected = "pausado" if providers_paused or serpro_paused else previous
    jobs = [
        job
        for job in registered_jobs()
        if job.name in {"federal-payments-monthly", "federal-sitfis-monthly"}
    ]
    assert len(jobs) == 2
    assert [job.last_error_code for job in jobs] == [expected, expected]
    response = _web(delivery, delivery.graph.actor).get("/gestao/automacoes/")
    assert response.status_code == 200
    assert response.content.decode().count('data-job-error="pausado"') == (
        2 if expected == "pausado" else 0
    )
