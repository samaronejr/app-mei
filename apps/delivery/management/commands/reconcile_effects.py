"""Replay the independent effect journal without transmitting any message."""

from typing import Any

from django.core.management.base import BaseCommand

from apps.delivery.replay import replay_journal


class Command(BaseCommand):
    """Leave DELIVERY_PAUSED enabled until the operator inspects this result."""

    help = "Replay independent SMTP receipts after restore; requires DELIVERY_PAUSED."

    def handle(self, *_args: Any, **_options: Any) -> str:  # noqa: ANN401 - Django API
        """Return reference-free counts only after every tenant succeeds."""
        count = replay_journal()
        return f"Replayed: {count}. DELIVERY_PAUSED remains enabled."
