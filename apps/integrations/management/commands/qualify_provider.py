"""Run registered non-live provider qualification and write minimized evidence."""

import json
from argparse import ArgumentParser
from pathlib import Path
from typing import TYPE_CHECKING

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.integrations.privacy import scrub_provider_text
from apps.integrations.serpro.qualification import SCENARIOS

if TYPE_CHECKING:
    from apps.integrations.transport_types import Environment


class Command(BaseCommand):
    """A closed scenario registry, never a live-taxpayer provider console."""

    help = "Qualifica cenarios simulados ou de teste, sem chamadas de producao."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Later provider todos register scenarios without widening this CLI."""
        parser.add_argument("provider")
        parser.add_argument("--scenario", required=True)
        parser.add_argument("--environment", required=True)

    def handle(self, *_args: object, **options: object) -> None:
        """Validate routing before creating records or opening a socket."""
        provider = str(options["provider"])
        name = str(options["scenario"])
        raw_environment = str(options["environment"])
        scenario = SCENARIOS.get((provider, name))
        if scenario is None or raw_environment not in {"fake", "trial", "homologacao"}:
            message = "cenario_ou_ambiente_nao_permitido"
            raise CommandError(message)
        environment: Environment
        match raw_environment:
            case "fake":
                environment = "fake"
            case "trial":
                environment = "trial"
            case "homologacao":
                environment = "homologacao"
            case _:
                message = "ambiente_nao_permitido"
                raise CommandError(message)
        if environment not in scenario.environments:
            message = "ambiente_nao_permitido"
            raise CommandError(message)
        evidence = scenario.run(environment)
        record = {
            "provider": provider,
            "scenario": name,
            "environment": environment,
            "request_digest": evidence.request_digest,
            "response_digest": evidence.response_digest,
            "http_status": evidence.http_status,
            "billing_class": evidence.billing_class,
            "parsed_fields": evidence.parsed_fields,
            "elapsed_seconds": evidence.elapsed_seconds,
        }
        destination = Path(".evidence")
        destination.mkdir(exist_ok=True)
        stamp = timezone.now().strftime("%Y%m%dT%H%M%S%fZ")
        path = destination / f"AXRM-PQ-{provider}-{name}-{stamp}.txt"
        path.write_text(scrub_provider_text(json.dumps(record, sort_keys=True)) + "\n")
        self.stdout.write(str(path))
