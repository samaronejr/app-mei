"""The CLI surface for reproducible offline metrics; never a promotion gate."""

import hashlib
import json
from uuid import UUID

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.automation.admission import enabled
from apps.automation.evaluation import evaluate, load_dataset
from apps.automation.llm import FakeLLM, LLMClient, configured_client
from apps.automation.models import EvalRun


class Command(BaseCommand):
    """Evaluate only committed synthetic data; real usage still needs owner gates."""

    help = "Evaluate the synthetic v1 set and emit metrics JSON."

    def add_arguments(self, parser: CommandParser) -> None:
        """Name the model, dataset and explicit opt-in scope for real providers."""
        parser.add_argument("--model", choices=["fake", "openai"], required=True)
        parser.add_argument("--set", choices=["v1"], required=True)
        parser.add_argument("--tenant-id", default="")

    def handle(self, *_args: str, **options: str) -> None:
        """Persist and print metrics, not prompts or per-document model output."""
        model = options["model"]
        client: LLMClient = FakeLLM()
        if model != "fake":
            tenant_id = options["tenant_id"]
            if (
                not tenant_id
                or model != settings.AI_PROVIDER
                or not enabled(UUID(tenant_id))
            ):
                msg = (
                    "Real evaluation requires configured provider "
                    "and approved ai_shadow scope."
                )
                raise CommandError(msg)
            client = configured_client(UUID(tenant_id))
        path = settings.BASE_DIR / "tests/fixtures/eval" / options["set"]
        digest = hashlib.sha256()
        for file in sorted(path.iterdir()):
            digest.update(file.name.encode())
            digest.update(file.read_bytes())
        metrics = evaluate(load_dataset(path), client)
        EvalRun.objects.create(
            model_id="fake-v1" if model == "fake" else settings.AI_MODEL,
            dataset_version=options["set"],
            dataset_hash=digest.hexdigest(),
            metrics=metrics,
        )
        self.stdout.write(json.dumps(metrics, sort_keys=True))
