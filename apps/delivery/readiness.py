"""Outbox budgets for the public probe; never return tenant or message data."""

from datetime import datetime

from django.conf import settings
from django.utils import timezone


def outbox_reason(oldest_queued_at: datetime | None, dead_letters: int) -> str:
    """Return the first exceeded budget; zero is a strict limit, not an opt-out."""
    if (
        oldest_queued_at is not None
        and (timezone.now() - oldest_queued_at).total_seconds()
        > settings.OUTBOX_MAX_QUEUED_AGE_SECONDS
    ):
        return "outbox_oldest_queued_age"
    if dead_letters > settings.OUTBOX_MAX_DEAD_LETTERS:
        return "outbox_dead_letters"
    return ""
