"""Retry pending LGPD encarregado notifications manually."""

from argparse import ArgumentParser
from typing import Any, Final

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.audit.models import DataSubjectRequest
from apps.lgpd import views as lgpd_views

DEFAULT_BATCH_SIZE: Final = 100
MAX_BATCH_SIZE: Final = 500


class Command(BaseCommand):
    """Notify the encarregado for requests with no successful delivery stamp."""

    help = "Retry pending LGPD notification deliveries."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Allow an operator to choose a smaller bounded batch."""
        parser.add_argument(
            "--batch-size",
            type=int,
            default=DEFAULT_BATCH_SIZE,
            help=(
                f"Requests to inspect (1-{MAX_BATCH_SIZE}; "
                f"default: {DEFAULT_BATCH_SIZE})."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401, ARG002
        """Send each pending notification once and stamp successful rows."""
        batch_size = int(options["batch_size"])
        if not 1 <= batch_size <= MAX_BATCH_SIZE:
            msg = f"--batch-size must be between 1 and {MAX_BATCH_SIZE}."
            raise CommandError(msg)

        failed = 0
        sent = 0
        pending = list(
            # PLATFORM_QUERY_OK: a host operator invokes this bounded command to
            # inspect pending statutory requests across tenants and anonymous rows.
            DataSubjectRequest.objects.filter(
                encarregado_notified_at__isnull=True,
            ).order_by("created_at", "pk")[:batch_size],
        )
        for dsr in pending:
            try:
                lgpd_views._notify_encarregado(dsr)  # noqa: SLF001
            except Exception as error:  # noqa: BLE001
                # PLATFORM_QUERY_OK: update the exact row selected by the bounded scan.
                DataSubjectRequest.objects.filter(pk=dsr.pk).update(
                    notification_last_error=str(error),
                )
                failed += 1
                continue
            # PLATFORM_QUERY_OK: stamp the exact row selected by the bounded scan.
            DataSubjectRequest.objects.filter(pk=dsr.pk).update(
                encarregado_notified_at=timezone.now(),
            )
            sent += 1

        if failed:
            msg = f"{failed} LGPD notification(s) failed; {sent} sent."
            raise CommandError(msg)
        self.stdout.write(self.style.SUCCESS(f"Sent {sent} LGPD notification(s)."))
