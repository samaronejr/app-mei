"""Celery tasks owned by the core app."""

from celery import shared_task


@shared_task
def ping() -> str:
    """Return a constant so the broker-and-worker round trip can be asserted."""
    return "pong"
