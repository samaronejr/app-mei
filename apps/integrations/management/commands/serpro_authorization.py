"""Operator surface for the firm authorization flow, with ordinary capability checks."""

import json
from argparse import ArgumentParser
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.accounts.models import User
from apps.authz.services import can
from apps.core.tenancy import tenant_context
from apps.integrations.models import ProviderConnection
from apps.integrations.serpro.adapter import Parties
from apps.integrations.serpro.authorization import TermoSubmission, submit_termo
from apps.integrations.serpro.models import ACTIVE_STATES, FirmIntegrationIssue
from apps.integrations.serpro.registration import MANAGE
from apps.integrations.serpro.termo import TermoDraft, legal_notices, unsigned_termo

if TYPE_CHECKING:
    from apps.integrations.transport_types import JSONValue


class Command(BaseCommand):
    """Generate unsigned XML, submit an already scanned upload, or list owned work."""

    help = (
        "Autorizar Axorim na Receita: preparar, enviar termo assinado "
        "ou listar pendencias."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        """No private key, certificate password or signing operation is accepted."""
        parser.add_argument("action", choices=("prepare", "submit", "issues"))
        parser.add_argument("--tenant", required=True, type=UUID)
        parser.add_argument("--actor", required=True)
        parser.add_argument("--connection", required=True, type=UUID)
        parser.add_argument("--expected-version", type=int)
        parser.add_argument("--document", type=UUID)
        parser.add_argument("--contratante")
        parser.add_argument("--firm")
        parser.add_argument("--contratante-name")
        parser.add_argument("--firm-name")
        parser.add_argument("--valid-until", type=date.fromisoformat)
        parser.add_argument("--legal-notices", type=Path)

    def handle(self, *_args: object, **options: object) -> None:
        """Use firm capability checks and previously scanned uploads."""
        tenant_id = UUID(str(options["tenant"]))
        actor = User.objects.get(pk=str(options["actor"]))
        connection_id = UUID(str(options["connection"]))
        with tenant_context(tenant_id):
            if not can(actor, MANAGE):
                raise PermissionDenied
            connection = ProviderConnection.objects.get(
                pk=connection_id, provider="serpro"
            )
            if options["action"] == "issues":
                for issue in FirmIntegrationIssue.objects.filter(
                    connection=connection, state__in=ACTIVE_STATES
                ).order_by("review_on", "pk"):
                    self.stdout.write(
                        json.dumps(
                            {
                                "id": str(issue.pk),
                                "reason": issue.reason,
                                "owner_id": str(issue.owner_id),
                                "review_on": str(issue.review_on),
                                "version": issue.version,
                            }
                        )
                    )
                return
        if not options["contratante"] or not options["firm"]:
            message = "identidades_obrigatorias"
            raise CommandError(message)
        firm = str(options["firm"])
        parties = Parties(str(options["contratante"]), firm, firm)
        if options["action"] == "submit":
            if options["document"] is None or options["expected_version"] is None:
                message = "documento_e_versao_obrigatorios"
                raise CommandError(message)
            result = submit_termo(
                actor,
                tenant_id,
                TermoSubmission(
                    connection_id,
                    UUID(str(options["document"])),
                    int(str(options["expected_version"])),
                    parties,
                ),
            )
            self.stdout.write(
                json.dumps(
                    {
                        "state": result.state,
                        "status": result.http_status,
                        "provider_code": result.provider_code,
                    }
                )
            )
            return
        if options["legal_notices"] is None or options["valid_until"] is None:
            message = "avisos_legais_e_validade_obrigatorios"
            raise CommandError(message)
        notices: JSONValue = json.loads(Path(str(options["legal_notices"])).read_text())
        draft = TermoDraft(
            str(options["contratante_name"] or ""),
            str(options["firm_name"] or ""),
            timezone.localdate(),
            date.fromisoformat(str(options["valid_until"])),
            legal_notices(notices),
        )
        self.stdout.write(unsigned_termo(parties, draft).decode())
