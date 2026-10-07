"""Monthly generation, the quarantine scan sweep (T12) and extraction (T13)."""

from collections.abc import Callable
from datetime import date
from typing import Final
from uuid import UUID

import sentry_sdk
from celery import shared_task
from django.db.models import Q
from django.utils import timezone

from apps.clients.models import Engagement
from apps.core.flags import tenant_has
from apps.core.tasks import (
    PhasedTenantTask,
    PlatformTask,
    TenantTask,
    assert_no_transaction,
)
from apps.cycles.bulk_runner import (
    BulkClaim,
    StaleBulkClaimError,
    claim_next,
    due_operations,
    execute_claim,
)
from apps.cycles.extraction.service import (
    due_divergent_links,
    due_extractions,
    extract_intake,
    propagate_divergent_link,
)
from apps.cycles.models import PlaybookVersion, VersionState
from apps.cycles.openers import evaluate_tenant_exceptions
from apps.cycles.scanning import due_intakes, scan_intake, scanner_from_settings
from apps.cycles.services.cycle import open_cycle
from apps.cycles.targeting import AppliesTo, matching_clients
from apps.obligations.models import SchedulerHeartbeat
from apps.tenants.models import Tenant

# One PDF gets 10 s of page-by-page deadline in `apps.cycles.parsing`; the soft limit
# adds the read and the recording around it and, unlike the page deadline, also stops
# a single page that never finishes. The hard limit is the backstop.
EXTRACTION_SOFT_LIMIT_SECONDS: Final = 15
EXTRACTION_HARD_LIMIT_SECONDS: Final = 30


@shared_task(base=PhasedTenantTask)
def run_bulk_operation(
    tenant_id: UUID | str,
    operation_id: UUID | str,
    *,
    after_item: Callable[[int], None] | None = None,
    after_claim: Callable[[BulkClaim], None] | None = None,
) -> int:
    """Process stored subjects; injected crash hooks run outside item transactions."""
    tid, operation = UUID(str(tenant_id)), UUID(str(operation_id))
    count = 0
    while (claim := claim_next(tid, operation)) is not None:
        if after_claim is not None:
            after_claim(claim)
        try:
            execute_claim(claim)
        except StaleBulkClaimError:
            # Another valid fence or cancellation won. Never replay this claim.
            continue
        count += 1
        if after_item is not None:
            after_item(count)
    return count


@shared_task(base=PlatformTask, bind=True)
def resume_bulk_operations(self: PlatformTask) -> int:
    """Recover interrupted database-only work, without stealing unexpired claims."""
    assert_no_transaction()
    count = 0
    for tid in self.tenant_ids():
        if tenant_has(tid, "cycles_r1") and tenant_has(tid, "bulk_r1"):
            for operation_id in due_operations(tid):
                count += run_bulk_operation(str(tid), str(operation_id))
    return count


@shared_task(base=TenantTask)
def generate_cycles_for_period(tenant_id: UUID | str, period: str) -> dict[str, int]:
    """Return accurate inserted/existing counts; an unflagged firm is untouched."""
    tenant_pk = UUID(str(tenant_id))
    counts = {"created": 0, "existing": 0}
    if not tenant_has(tenant_pk, "cycles_r1"):
        return counts
    on = date.fromisoformat(period)
    if on.day != 1:
        msg = "period must start on day 1"
        raise ValueError(msg)
    # PLATFORM_QUERY_OK: the TenantTask context names this exact firm; generation
    # never enumerates tenant data outside that explicit context.
    tenant = Tenant.objects.get(pk=tenant_pk)
    definitions = (
        PlaybookVersion.objects.filter(state=VersionState.PUBLISHED)
        .order_by("playbook_id", "-number")
        .distinct("playbook_id")
    )
    for definition in definitions:
        scope = AppliesTo.parse(definition.applies_to_json)
        clients = matching_clients(tenant, definition.applies_to_json, on)
        engagements = Engagement.objects.filter(
            client_id__in=clients.values("pk"), starts_on__lte=on
        ).filter(Q(ends_on__isnull=True) | Q(ends_on__gt=on))
        if scope.service_kinds:
            engagements = engagements.filter(service_kind__in=scope.service_kinds)
        for engagement in engagements.order_by("pk"):
            ref = open_cycle(
                None, engagement.pk, on, definition.pk, idempotency_key=f"period:{on}"
            )
            counts["created" if ref.created else "existing"] += 1
    return counts


@shared_task(base=PlatformTask, bind=True)
def generate_monthly_cycles(self: PlatformTask) -> dict[str, int]:
    """Beat calls the TenantTask only for enabled tenants, then stamps success.

    This is database-only work: synchronous task calls preserve failure reporting.
    A failed tenant prevents the global heartbeat from falsely claiming success.
    """
    counts = {"created": 0, "existing": 0}
    period = timezone.localdate().replace(day=1).isoformat()
    for tenant in self.each_tenant():
        if tenant_has(tenant.pk, "cycles_r1"):
            result = generate_cycles_for_period(str(tenant.pk), period)
            for key in counts:
                counts[key] += result[key]
    SchedulerHeartbeat.objects.update_or_create(name="generate_cycles", defaults={})
    return counts


@shared_task(base=PhasedTenantTask)
def scan_tenant_quarantine(
    tenant_id: UUID | str, *, vault_only: bool = False
) -> dict[str, int]:
    """Scan one firm's due intakes; each scan runs phases A-B-C on its own.

    No outer transaction (PhasedTenantTask): the scanner's socket I/O happens between
    the per-intake DB phases. One failing intake does not starve the others, and is
    re-raised at the end rather than swallowed.
    """
    tenant_pk = UUID(str(tenant_id))
    now = timezone.now()
    scanner = scanner_from_settings()
    counts: dict[str, int] = {}
    failures: list[BaseException] = []
    for intake_id in due_intakes(tenant_pk, now, vault_only=vault_only):
        try:
            outcome = scan_intake(tenant_pk, intake_id, scanner=scanner, now=now)
        except Exception as error:  # noqa: BLE001 - reported below, never dropped
            sentry_sdk.capture_exception(error)
            failures.append(error)
            continue
        counts[outcome.value] = counts.get(outcome.value, 0) + 1
    if failures:
        msg = f"{len(failures)} intake scan(s) failed in tenant {tenant_pk}"
        raise RuntimeError(msg) from failures[0]
    return counts


@shared_task(base=TenantTask)
def open_exceptions_for_tenant(tenant_id: UUID | str) -> dict[str, int]:
    """Run T14's deterministic openers for one firm; unflagged firms are untouched."""
    tenant_pk = UUID(str(tenant_id))
    if not tenant_has(tenant_pk, "cycles_r1"):
        return {"evaluated": 0, "skipped_no_owner": 0}
    # PLATFORM_QUERY_OK: the TenantTask context names this exact firm.
    counts = evaluate_tenant_exceptions(Tenant.objects.get(pk=tenant_pk))
    return {"evaluated": counts.evaluated, "skipped_no_owner": counts.skipped_no_owner}


@shared_task(base=PlatformTask, bind=True)
def open_deterministic_exceptions(self: PlatformTask) -> dict[str, int]:
    """Beat: once a day, only for firms with `cycles_r1`."""
    totals = {"evaluated": 0, "skipped_no_owner": 0}
    for tid in self.tenant_ids():
        if tenant_has(tid, "cycles_r1"):
            for key, value in open_exceptions_for_tenant(str(tid)).items():
                totals[key] += value
    return totals


@shared_task(base=PlatformTask, bind=True)
def scan_quarantine(self: PlatformTask) -> int:
    """Beat: every minute, scan due intakes; request uploads only under `cycles_r1`.

    Portal vault uploads are scanned for every firm (T13, plan C-09): the vault is not
    behind the flag, so its quarantine cannot be either -- an unscanned vault file is
    never served to the portal, and without the sweep it would never be.
    """
    assert_no_transaction()
    scanned = 0
    for tid in self.tenant_ids():
        flagged = tenant_has(tid, "cycles_r1")
        scanned += sum(
            scan_tenant_quarantine(str(tid), vault_only=not flagged).values()
        )
    return scanned


@shared_task(
    base=PhasedTenantTask,
    soft_time_limit=EXTRACTION_SOFT_LIMIT_SECONDS,
    time_limit=EXTRACTION_HARD_LIMIT_SECONDS,
)
def extract_document(tenant_id: UUID | str, intake_id: UUID | str) -> str:
    """Extract one verified intake; a soft-limit hit is recorded as interrupted."""
    return extract_intake(
        UUID(str(tenant_id)), UUID(str(intake_id)), now=timezone.now()
    ).value


@shared_task(base=PlatformTask, bind=True)
def extract_documents(self: PlatformTask) -> int:
    """Beat: queue extraction and late-link findings for `cycles_r1` firms.

    Each document is its own task so the soft limit bounds one document. A document
    queued twice is harmless: the second run finds the intake no longer
    `verificado` (or at a newer version) and writes nothing.
    Divergent intakes are never parsed again: missing per-cycle findings have
    their own database-only task, including links committed after classification.
    """
    assert_no_transaction()
    queued = 0
    for tid in self.tenant_ids():
        if tenant_has(tid, "cycles_r1"):
            for intake_id in due_extractions(tid):
                extract_document.delay(str(tid), str(intake_id))
                queued += 1
            for link_id in due_divergent_links(tid):
                propagate_document_link.delay(str(tid), str(link_id))
                queued += 1
    return queued


@shared_task(base=TenantTask)
def propagate_document_link(tenant_id: UUID | str, link_id: UUID | str) -> bool:
    """Converge a divergent document's late link on one internal cycle exception."""
    return propagate_divergent_link(
        UUID(str(tenant_id)), UUID(str(link_id)), now=timezone.now()
    )
