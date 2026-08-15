"""Report inconsistencies between all tenant document rows and object storage."""

import hashlib
from argparse import ArgumentParser
from dataclasses import dataclass, field
from typing import Any, Final

from django.core.files.storage import Storage, default_storage
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.obligations.models import Document
from apps.tenants.models import Tenant

DEFAULT_HASH_SAMPLE_SIZE: Final = 20
MAX_HASH_SAMPLE_SIZE: Final = 100
PROBE_PREFIX: Final = "_probe/"


@dataclass(slots=True)
class PhaseOneResult:
    """The complete global row union and its per-tenant integrity findings."""

    storage_keys: set[str] = field(default_factory=set)
    critical_reports: list[str] = field(default_factory=list)
    processed_tenants: int = 0
    documents: int = 0
    missing: int = 0
    hashes_checked: int = 0
    hash_mismatches: int = 0


def _collect_global_union(storage: Storage, hash_sample_size: int) -> PhaseOneResult:
    """Collect every tenant's keys while each tenant context is active."""
    result = PhaseOneResult()
    # PLATFORM_QUERY_OK: a host operator must enumerate every tenancy root before any
    # tenant-scoped document query can be made; omitting one would corrupt the union.
    for tenant in Tenant.objects.all().iterator():
        tenant_hashes_checked = 0
        with tenant_context(tenant.id):
            rows = Document.objects.order_by("storage_key").values_list(
                "storage_key",
                "sha256",
            )
            for storage_key, expected_sha256 in rows:
                result.storage_keys.add(storage_key)
                result.documents += 1
                if not storage.exists(storage_key):
                    result.missing += 1
                    result.critical_reports.append(
                        "CRITICAL row-without-object "
                        f"tenant={tenant.id} storage_key={storage_key}",
                    )
                    continue
                if tenant_hashes_checked >= hash_sample_size:
                    continue

                with storage.open(storage_key, "rb") as stored:
                    actual_sha256 = hashlib.file_digest(stored, "sha256").hexdigest()
                tenant_hashes_checked += 1
                result.hashes_checked += 1
                if actual_sha256 != expected_sha256:
                    result.hash_mismatches += 1
                    result.critical_reports.append(
                        "CRITICAL sha256-mismatch "
                        f"tenant={tenant.id} storage_key={storage_key}",
                    )
        result.processed_tenants += 1
    return result


def _bucket_keys(storage: Storage) -> set[str]:
    """List the bucket once and exclude the probe namespace from comparison."""
    _directories, files = storage.listdir("")
    return {name for name in files if not name.startswith(PROBE_PREFIX)}


def _orphan_reports(storage: Storage, orphan_keys: set[str]) -> list[str]:
    """Render immutable-object age from the backend's last-modified timestamp."""
    reports: list[str] = []
    for storage_key in sorted(orphan_keys):
        modified_at = storage.get_modified_time(storage_key)
        age_seconds = max(0, int((timezone.now() - modified_at).total_seconds()))
        reports.append(
            f"ORPHAN storage_key={storage_key} age_seconds={age_seconds}",
        )
    return reports


class Command(BaseCommand):
    """Collect every tenant before comparing the complete union to one listing."""

    help = "Report document row/object inconsistencies without modifying storage."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Register the bounded per-tenant authenticated-read sample size."""
        parser.add_argument(
            "--hash-sample-size",
            type=int,
            default=DEFAULT_HASH_SAMPLE_SIZE,
            help=(
                "Authenticated SHA-256 reads per tenant "
                f"(default: {DEFAULT_HASH_SAMPLE_SIZE}, max: {MAX_HASH_SAMPLE_SIZE})."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401, ARG002
        """Run the two phases and print findings only after safety guards pass."""
        hash_sample_size = int(options["hash_sample_size"])
        if not 0 <= hash_sample_size <= MAX_HASH_SAMPLE_SIZE:
            msg = f"--hash-sample-size must be between 0 and {MAX_HASH_SAMPLE_SIZE}."
            raise CommandError(msg)

        # PLATFORM_QUERY_OK: this host-level completeness assertion counts every
        # tenancy root so a shortened iteration aborts before classifying bucket keys.
        expected_tenants = Tenant.objects.count()
        try:
            phase_one = _collect_global_union(default_storage, hash_sample_size)
        except Exception as error:
            msg = "Phase 1 failed; orphan report suppressed."
            raise CommandError(msg) from error

        if phase_one.processed_tenants != expected_tenants:
            msg = (
                f"Refusing incomplete union: processed {phase_one.processed_tenants} "
                f"of {expected_tenants} tenants; orphan report suppressed."
            )
            raise CommandError(msg)

        bucket_keys = _bucket_keys(default_storage)
        if not phase_one.storage_keys and bucket_keys:
            msg = "Refusing empty document union for a non-empty bucket."
            raise CommandError(msg)

        orphan_keys = bucket_keys - phase_one.storage_keys
        orphan_reports = _orphan_reports(default_storage, orphan_keys)

        self.stdout.write(
            f"PHASE 1 tenants={phase_one.processed_tenants} "
            f"documents={phase_one.documents} missing={phase_one.missing} "
            f"hashes_checked={phase_one.hashes_checked} "
            f"hash_mismatches={phase_one.hash_mismatches}",
        )
        for report in phase_one.critical_reports:
            self.stdout.write(report)
        self.stdout.write(
            f"PHASE 2 bucket_keys={len(bucket_keys)} "
            f"orphan_candidates={len(orphan_keys)}",
        )
        for report in orphan_reports:
            self.stdout.write(report)
        self.stdout.write("COMPLETE report-only")
