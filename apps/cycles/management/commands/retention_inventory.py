"""`manage.py retention_inventory`: list what retention would keep or dispose of.

AXRM-051. Read-only by default whatever the purge switch says. `--purge` runs the
live purge for the whole tenant only when RETENTION_PURGE_DRY_RUN is off; it is
refused while the switch is on and with `--client`, so a scoped listing can never
dispose of anything.
"""

from typing import Any
from uuid import UUID

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from apps.cycles.retention_inventory import inventory_lines
from apps.cycles.retention_tasks import purge_tenant


class Command(BaseCommand):
    """Print the inventory, or run the tenant's live purge when explicitly allowed."""

    help = "List the retention inventory of one tenant (read-only by default)."

    def add_arguments(self, parser: CommandParser) -> None:
        """Register the tenant, an optional client and the explicit purge flag."""
        parser.add_argument("--tenant", type=UUID, required=True)
        parser.add_argument("--client", type=UUID)
        parser.add_argument("--purge", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401, ARG002
        """Refuse a purge the switch or the client scope forbids; else list."""
        tenant_id: UUID = options["tenant"]
        client_id: UUID | None = options["client"]
        now = timezone.now()
        if options["purge"]:
            if settings.RETENTION_PURGE_DRY_RUN:
                msg = "--purge recusado: RETENTION_PURGE_DRY_RUN está ligado."
                raise CommandError(msg)
            if client_id is not None:
                msg = "--purge recusado com --client: a remoção é por escritório."
                raise CommandError(msg)
            counts = purge_tenant(tenant_id, dry_run=False, now=now)
            self.stdout.write(
                "PURGE " + " ".join(f"{key}={value}" for key, value in counts.items())
            )
            return
        for line in inventory_lines(tenant_id, now=now, client_id=client_id):
            self.stdout.write(line)
