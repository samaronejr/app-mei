"""The Celery wiring must be loss-averse, because these sweeps are fiscal."""

from django.conf import settings

from apps.core.tasks import ping
from config import celery_app


def test_ping_returns_pong() -> None:
    # Given the wiring-proof task
    # When it is executed
    result = ping()

    # Then it returns the constant the round-trip assertion looks for
    assert result == "pong"


def test_broker_and_backend_come_from_redis_url() -> None:
    # Given the configured Celery app
    # When its transport settings are read
    # Then both point at REDIS_URL rather than an in-memory default
    assert celery_app.conf.broker_url == settings.REDIS_URL
    assert celery_app.conf.result_backend == settings.REDIS_URL


def test_tasks_are_acknowledged_late_and_requeued_on_worker_loss() -> None:
    # Given the configured Celery app
    # When the delivery guarantees are read
    # Then a task killed mid-flight is redelivered rather than silently dropped
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True


def test_beat_uses_the_database_scheduler() -> None:
    # Given the configured Celery app
    # When the beat scheduler is read
    # Then schedules live in the database, not in a file only beat can see
    assert (
        celery_app.conf.beat_scheduler
        == "django_celery_beat.schedulers:DatabaseScheduler"
    )


def test_schedules_are_expressed_in_brazilian_local_time() -> None:
    # Given the configured Celery app
    # When its timezone is read
    # Then it matches the Django timezone while timestamps stay UTC
    assert celery_app.conf.timezone == "America/Sao_Paulo"
    assert celery_app.conf.enable_utc is True
