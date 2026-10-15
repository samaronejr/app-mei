"""Build the versioned, synthetic-only 300-document evaluation set."""

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.automation.dataset import build_dataset


class Command(BaseCommand):
    """Generate v1 without reading storage, tenants or real documents."""

    help = "Generate the committed synthetic v1 evaluation set."

    def handle(self, *_args: str, **_options: str) -> None:
        """Write the small fixture set at its single fixed location."""
        target = settings.BASE_DIR / "tests/fixtures/eval/v1"
        build_dataset(target)
        self.stdout.write(f"300 synthetic documents: {target}")
