"""Operator-only Focus capture disable with read-back (A6 FC-EMP, A7.3, A11)."""

from argparse import ArgumentParser
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError

from apps.integrations.call_contracts import AdmissionDeniedError
from apps.integrations.focus.company import FocusRefusedError, plan_disable, run_disable
from apps.integrations.focus.operator import (
    admission_message,
    emit,
    outcome_record,
    write_receipt,
)


class Command(BaseCommand):
    """Turn a client's Focus receipt switches off and confirm it by reading back."""

    help = (
        "Desativa a captura de documentos recebidos de um cliente na Focus e "
        "confirma pela leitura de volta."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        """`--stream` may repeat; without it every configured stream stops."""
        parser.add_argument("--client", required=True, type=UUID)
        parser.add_argument("--stream", action="append", default=[])

    def handle(self, *_args: object, **options: object) -> None:
        """Stop capture locally first, then at Focus; never report unconfirmed."""
        raw = options["stream"]
        streams = [str(item) for item in raw] if isinstance(raw, list) else []
        try:
            plan = plan_disable(UUID(str(options["client"])), streams or None)
            outcome = run_disable(plan)
        except FocusRefusedError as error:
            raise CommandError(str(error)) from None
        except AdmissionDeniedError as error:
            raise CommandError(admission_message(error)) from None
        record = {"streams": list(outcome.streams), **outcome_record(outcome)}
        emit(self.stdout, record)
        path = write_receipt(
            "disable",
            {
                "link": str(plan.link_id),
                "environment": plan.environment,
                "fields": outcome.sent,
                **record,
            },
        )
        self.stdout.write(str(path))
        # A confirmed whole-link read-back has already moved the link to
        # `desativada_no_provedor_confirmada` (run_disable).
        if not outcome.confirmed:
            pending = ", ".join(outcome.unconfirmed) or outcome.disablement_state
            message = (
                "Desativação não confirmada pela leitura de volta "
                f"({pending}); a captura já está parada localmente."
            )
            raise CommandError(message)
        self.stdout.write("Desativação confirmada pela leitura de volta.")
