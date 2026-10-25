"""Restore provider receipts without issuing any provider request."""

from argparse import ArgumentParser
from typing import Any
from uuid import UUID

from django.core.exceptions import ObjectDoesNotExist
from django.core.management.base import BaseCommand, CommandError

from apps.integrations.call_contracts import AdmissionDeniedError, IntentConflictError
from apps.integrations.journal_schema import JournalError
from apps.integrations.provider_replay import reconcile


class Command(BaseCommand):
    """Keep providers paused and reconcile the complete independent journal."""

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Require an opaque restore identity, never free text or credentials."""
        parser.add_argument("--restore-ref", required=True, type=UUID)

    def handle(self, *_args: Any, **options: Any) -> None:  # noqa: ANN401
        """Only report completion after every scope and attempt was reconciled."""
        try:
            count = reconcile(UUID(str(options["restore_ref"])))
        except (
            JournalError,
            IntentConflictError,
            AdmissionDeniedError,
            ObjectDoesNotExist,
        ):
            message = "Provider reconciliation incomplete; keep PROVIDERS_PAUSED."
            raise CommandError(message) from None
        self.stdout.write(
            f"provider_attempts_restored={count}; providers remain paused"
        )
