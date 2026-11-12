"""Operator-only Focus company setup (A6 FC-EMP); never reachable from the web.

Certificates are registered in Focus's own interface by the firm or the client
(owner answer Q1): this command takes no certificate, key or password, and sends
only the plan's receipt switches and start dates.
"""

from argparse import ArgumentParser
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError

from apps.integrations.call_contracts import AdmissionDeniedError
from apps.integrations.focus.company import FocusRefusedError, plan_setup, run_setup
from apps.integrations.focus.operator import (
    admission_message,
    emit,
    outcome_record,
    write_receipt,
)


class Command(BaseCommand):
    """Configure received-document capture for one client at Focus."""

    help = (
        "Configura a captura de documentos recebidos de um cliente na Focus "
        "(sem certificado: ele é cadastrado na própria Focus)."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Exactly the plan's options; nothing can carry key material."""
        parser.add_argument("--client", required=True, type=UUID)
        parser.add_argument("--streams", required=True)
        parser.add_argument("--start-month", required=True)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *_args: object, **options: object) -> None:
        """Plan (refusing before any write), print the field set, then run it."""
        dry_run = bool(options["dry_run"])
        try:
            plan = plan_setup(
                UUID(str(options["client"])),
                str(options["streams"]),
                str(options["start_month"]),
            )
        except FocusRefusedError as error:
            raise CommandError(str(error)) from None
        emit(
            self.stdout,
            {
                "client": str(plan.client_id),
                "environment": plan.environment,
                "streams": list(plan.streams),
                "start_month": plan.start_month.isoformat(),
                "fields": plan.fields,
                "dry_run": dry_run,
            },
        )
        try:
            outcome = run_setup(plan, dry_run=dry_run)
        except AdmissionDeniedError as error:
            raise CommandError(admission_message(error)) from None
        record = outcome_record(outcome)
        emit(self.stdout, record)
        if not dry_run:
            path = write_receipt(
                "setup",
                {
                    "client": str(plan.client_id),
                    "link": str(plan.link_id),
                    "environment": plan.environment,
                    "fields": plan.fields,
                    "qualification_ref": plan.qualification_ref,
                    **record,
                },
            )
            self.stdout.write(str(path))
        if dry_run:
            if not outcome.write or outcome.write.state != "sucesso":
                message = "A Focus não validou a configuração simulada."
                raise CommandError(message)
            self.stdout.write("Simulação validada pela Focus; nada foi alterado.")
            return
        if not outcome.confirmed:
            pending = ", ".join(outcome.unconfirmed)
            message = f"Configuração não confirmada pela leitura de volta: {pending}."
            raise CommandError(message)
        self.stdout.write("Configuração confirmada pela leitura de volta.")
