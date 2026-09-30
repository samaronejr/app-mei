"""Extract one clean intake and apply the wrong-company rule (T13, S15 B, S6.3).

Three phases, like the scan (`apps.cycles.scanning`), so no parse and no storage read
runs inside a transaction:

- A `claim_extraction` reads a `verificado` intake and its client's CNPJ in
  `tenant_context`;
- B the bytes are read from the final key, checked against the document's sha256 and
  handed to `apps.cycles.extraction.extract`, which parses only through the T12
  wrappers. A Celery soft time limit that fires here (one slow PDF page) is answered
  as "extração interrompida", never as a crash;
- C `record_extraction` locks the intake, refuses a stale ticket without writing,
  stores the suggested fields and classifies the intake.

Wrong company: when the document names valid CNPJs and none of them is the client's
own, every CNPJ it names is neither the client nor a counterpart of the client, so the
document is another company's. The intake becomes `divergente` (its links stop
counting as sent, the portal stops serving it) and a `documento_de_outra_empresa`
exception opens on every linked cycle. Nothing is reassigned to any other client and
nothing is written into the document or its bytes.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

from celery.exceptions import SoftTimeLimitExceeded
from django.core.files.storage import Storage, default_storage
from django.utils import timezone

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.tasks import assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.cycles.evidence import (
    DocumentIntake,
    EvidenceLink,
    ExtractedField,
    IntakeState,
)
from apps.cycles.extraction import (
    DIVERGENT,
    EXTRACTOR_VERSION,
    INTERRUPTED,
    UNREADABLE,
    Extraction,
    extract,
    kind_for,
)
from apps.cycles.records import CycleEvent, ServiceCycle
from apps.cycles.services.exceptions import open_exception
from apps.fiscal.validators import normalize_document

EXCEPTION_REASON: Final = "documento_de_outra_empresa"
INTEGRITY: Final = "integridade"


class ExtractionOutcome(StrEnum):
    """What one extraction attempt did."""

    CLASSIFIED = "classificado"
    DIVERGENT = "divergente"
    STALE = "stale"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class ExtractionTicket:
    """Phase A's snapshot: the verified intake, at its version, and the client CNPJ."""

    tenant_id: UUID
    intake: DocumentIntake
    storage_key: str
    sha256: str
    client_cnpj: str


def claim_extraction(tenant_id: UUID, intake_id: UUID) -> ExtractionTicket | None:
    """Phase A: read a `verificado` intake inside the tenant's context, or None."""
    with tenant_context(tenant_id):
        intake = (
            DocumentIntake.objects.select_related("document")
            .filter(pk=intake_id, state=IntakeState.VERIFIED)
            .first()
        )
        if intake is None:
            return None
        cnpj = ClientCompany.objects.values_list("cnpj", flat=True).get(
            pk=intake.client_id
        )
        return ExtractionTicket(
            tenant_id=tenant_id,
            intake=intake,
            storage_key=intake.document.storage_key,
            sha256=intake.document.sha256,
            client_cnpj=normalize_document(cnpj),
        )


def run_extraction(ticket: ExtractionTicket, storage: Storage) -> Extraction:
    """Phase B: read the clean bytes and extract, outside any transaction."""
    assert_no_transaction()
    kind = kind_for(ticket.intake.sniffed_type)
    try:
        with storage.open(ticket.storage_key, "rb") as handle:
            payload: bytes = handle.read()
        if hashlib.sha256(payload).hexdigest() != ticket.sha256:
            return Extraction(kind=kind, note=f"{UNREADABLE}:{INTEGRITY}")
        return extract(ticket.intake, payload)
    except SoftTimeLimitExceeded:
        # The worker's soft limit fired inside one page (pypdf's own deadline is
        # only checked between pages): stop, and leave the file to a person.
        return Extraction(kind=kind, note=INTERRUPTED)


def divergent(client_cnpj: str, extraction: Extraction) -> bool:
    """S15 B: the document names companies, and the client is not one of them."""
    return bool(extraction.parties) and client_cnpj not in extraction.parties


def _linked_cycles(intake: DocumentIntake) -> list[tuple[ServiceCycle, UUID]]:
    authors: dict[UUID, UUID] = {}
    for cycle_id, author_id in (
        EvidenceLink.objects.filter(document_id=intake.document_id)
        .order_by("created_at", "pk")
        .values_list("request__cycle_id", "request__created_by_id")
    ):
        authors.setdefault(cycle_id, author_id)
    cycles = ServiceCycle.objects.select_for_update().filter(pk__in=list(authors))
    return [(cycle, authors[cycle.pk]) for cycle in cycles.order_by("pk")]


def record_extraction(
    ticket: ExtractionTicket, extraction: Extraction, *, now: datetime
) -> ExtractionOutcome:
    """Phase C: store the suggestions and classify, under the intake row lock."""
    with tenant_context(ticket.tenant_id):
        intake = (
            DocumentIntake.objects.select_for_update()
            .filter(pk=ticket.intake.pk)
            .first()
        )
        if (
            intake is None
            or intake.version != ticket.intake.version
            or intake.state != IntakeState.VERIFIED
        ):
            return ExtractionOutcome.STALE
        ExtractedField.objects.bulk_create(
            ExtractedField(
                tenant_id=intake.tenant_id,
                client_id=intake.client_id,
                document_id=intake.document_id,
                name=field.name,
                value=field.value,
                origin=field.origin,
                source_span=field.source_span,
                extractor_version=EXTRACTOR_VERSION,
                created_at=now,
            )
            for field in extraction.fields
        )
        is_divergent = divergent(ticket.client_cnpj, extraction)
        intake.state = IntakeState.DIVERGENT if is_divergent else IntakeState.CLASSIFIED
        intake.reason = DIVERGENT if is_divergent else extraction.note[:64]
        intake.version += 1
        intake.save(update_fields=["state", "reason", "version"])
        for cycle, author_id in _linked_cycles(intake):
            CycleEvent.objects.create(
                tenant_id=cycle.tenant_id,
                client_id=cycle.client_id,
                cycle=cycle,
                actor=None,
                type="document.extracted",
                aggregate_type="DocumentIntake",
                aggregate_id=intake.pk,
                aggregate_version=intake.version,
                occurred_at=now,
                payload={
                    "document_id": str(intake.document_id),
                    "state": intake.state,
                    "fields": len(extraction.fields),
                    "note": intake.reason,
                },
            )
            if is_divergent:
                open_exception(
                    EXCEPTION_REASON,
                    intake.pk,
                    actor=None,
                    cycle_id=cycle.pk,
                    expected_version=cycle.version,
                    owner=cycle.owner or User.objects.get(pk=author_id),
                    review_on=timezone.localdate(now),
                    evidence_refs=(intake.document_id,),
                )
        return (
            ExtractionOutcome.DIVERGENT
            if is_divergent
            else ExtractionOutcome.CLASSIFIED
        )


def extract_intake(
    tenant_id: UUID,
    intake_id: UUID,
    *,
    now: datetime,
    storage: Storage = default_storage,
) -> ExtractionOutcome:
    """Run phases A, B and C for one intake; `now` is injected."""
    assert_no_transaction()
    ticket = claim_extraction(tenant_id, intake_id)
    if ticket is None:
        return ExtractionOutcome.SKIPPED
    extraction = run_extraction(ticket, storage)
    return record_extraction(ticket, extraction, now=now)


def due_extractions(tenant_id: UUID, limit: int = 50) -> list[UUID]:
    """List the verified intakes still waiting for extraction, oldest first."""
    with tenant_context(tenant_id):
        return list(
            DocumentIntake.objects.filter(state=IntakeState.VERIFIED)
            .order_by("created_at", "pk")
            .values_list("pk", flat=True)[:limit]
        )
