"""AXRM-018: both federal sweeps are scheduled at their planned beat times.

Plan todo 18: the SITFIS sweep runs daily at 06:10 and the PAGTOWEB sweep at 06:20,
after the 06:00 deterministic-exceptions job; the docs state those times. The beat
schedule is machine-consumed configuration, so its entries are asserted.
"""

from celery.schedules import crontab
from django.conf import settings

ENTRIES = {
    "federal-sitfis-monthly": (
        "apps.integrations.serpro.tasks.schedule_situation_reports",
        crontab(hour=6, minute=10),
    ),
    "federal-payments-monthly": (
        "apps.integrations.serpro.tasks.schedule_payment_consultations",
        crontab(hour=6, minute=20),
    ),
}


def test_each_federal_sweep_has_its_task_and_planned_daily_time() -> None:
    schedule = settings.CELERY_BEAT_SCHEDULE
    for name, (task, when) in ENTRIES.items():
        entry = schedule[name]
        assert entry["task"] == task, name
        assert entry["schedule"] == when, (name, entry["schedule"])
