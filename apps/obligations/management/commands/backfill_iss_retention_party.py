"""Reconcile the ISS party before re-enabling consumers after a code revert."""

from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from apps.obligations.iss_retention_backfill_v1 import (
    BackfillRefusedError,
    backfill_iss_retention_party,
)


class Command(BaseCommand):
    """Operator catch-up command for code-only rollbacks."""

    help = "Reproject ISS parties with capture, imports and retention purges paused."

    def handle(self, *_args: object, **_options: object) -> None:
        """Run the same idempotent backfill as the data migration."""
        try:
            count = backfill_iss_retention_party(connections["default"])
        except BackfillRefusedError as error:
            raise CommandError(str(error)) from error
        self.stdout.write(f"ISS parties updated: {count}")
