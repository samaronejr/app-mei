"""Append an accountant's decision on one exact fiscal revision and its bytes."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Final

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404
from django.utils import timezone

from apps.authz.support import in_support_mode
from apps.clients.models import ClientCompany
from apps.core.flags import tenant_has
from apps.core.tenancy import tenant_context
from apps.cycles.capabilities import CYCLES_REVIEW
from apps.cycles.errors import VersionConflict
from apps.cycles.evidence import CLEAN_STATES, ScanVerdict
from apps.cycles.fiscal_records import FiscalDocumentReview
from apps.cycles.records import CycleEvent, ServiceCycle
from apps.cycles.services.cycle import (
    check_version,
    load_cycle,
    require_actor,
    tenant_id,
)
from apps.obligations.models import Document, FiscalDocument, RevisionArtifact

if TYPE_CHECKING:
    from uuid import UUID

    from apps.accounts.models import User

DECISIONS: Final = (
    ("aceito", "Aceito pela equipe"),
    ("rejeitado", "Não aceito"),
    ("requer_xml", "Requer XML completo"),
    ("documento_de_terceiro", "Documento de terceiro"),
)
NOTE_MAX_LENGTH: Final = 2000


def _review_input(fiscal: FiscalDocument) -> tuple[Document, str]:
    revision = fiscal.current_revision
    artifact = (
        RevisionArtifact.objects.select_related("document")
        .filter(
            revision_id=fiscal.current_revision_id,
            role="resumo" if fiscal.summary_only else "xml_original",
        )
        .order_by("pk")
        .first()
    )
    if revision is None or artifact is None:
        message = "O original desta revisão ainda não foi retido."
        raise ValidationError(message)
    digest = hashlib.sha256(
        f"{revision.provider_version}:{artifact.document.sha256}".encode()
    ).hexdigest()
    return artifact.document, digest


def input_digest(fiscal: FiscalDocument) -> str:
    """Bind the provider version to the exact retained original shown to staff."""
    with tenant_context(fiscal.tenant_id):
        return _review_input(fiscal)[1]


def _require_reviewable(
    fiscal: FiscalDocument,
    cycle: ServiceCycle,
    original: Document,
    decision: str,
) -> None:
    period = fiscal.issue_date if fiscal.family == "nfe" else fiscal.competence
    if (
        period is None
        or not cycle.period_start <= period <= cycle.period_end
        or fiscal.role_verification == "divergent"
    ):
        message = "O documento não pertence a este cliente e período."
        raise ValidationError(message)
    if (
        original.purged_at is not None
        or not original.intakes.filter(
            state__in=CLEAN_STATES,
            verdict=ScanVerdict.CLEAN,
            scanned_at__isnull=False,
        ).exists()
    ):
        message = "O original precisa passar pela verificação antivírus."
        raise ValidationError(message)
    if decision == "aceito" and (
        fiscal.summary_only
        or fiscal.role_verification != "confirmed"
        or fiscal.fiscal_state == "incerta"
    ):
        message = "Aceitar exige XML completo e atribuição confirmada."
        raise ValidationError(message)


def record_fiscal_document_review(  # noqa: PLR0913 - exact review-form contract
    actor: User,
    cycle_id: UUID,
    document_id: UUID,
    *,
    revision_id: UUID,
    decision: str,
    input_digest: str,
    expected_version: int,
    note: str = "",
) -> FiscalDocumentReview:
    """Lock client, fiscal identity and cycle, then append without changing history."""
    with transaction.atomic():
        if not tenant_has(tenant_id(), "document_capture"):
            raise Http404
        initial = load_cycle(actor, cycle_id)
        ClientCompany.objects.select_for_update().get(pk=initial.client_id)
        fiscal = (
            FiscalDocument.objects.select_for_update()
            .filter(pk=document_id, client_id=initial.client_id)
            .first()
        )
        if fiscal is None:
            raise Http404
        cycle = load_cycle(actor, cycle_id, lock=True)
        require_actor(actor, CYCLES_REVIEW)
        if in_support_mode():
            raise PermissionDenied
        check_version(cycle.version, expected_version)
        if fiscal.current_revision_id != revision_id:
            message = "A revisão fiscal mudou. Recarregue antes de salvar."
            raise VersionConflict(message)
        original, actual_digest = _review_input(fiscal)
        if input_digest != actual_digest:
            message = "O original mudou. Recarregue antes de salvar."
            raise VersionConflict(message)
        if decision not in dict(DECISIONS) or len(note) > NOTE_MAX_LENGTH:
            message = "Escolha uma decisão e use uma nota de até 2000 caracteres."
            raise ValidationError(message)
        _require_reviewable(fiscal, cycle, original, decision)
        if FiscalDocumentReview.objects.filter(
            revision_id=revision_id, cycle=cycle
        ).exists():
            message = "Esta revisão já recebeu uma decisão neste ciclo."
            raise VersionConflict(message)
        review = FiscalDocumentReview.objects.create(
            tenant_id=cycle.tenant_id,
            client_id=cycle.client_id,
            document=fiscal,
            revision_id=revision_id,
            cycle=cycle,
            decision=decision,
            reviewer=actor,
            input_digest=input_digest,
            note=note.strip(),
            reviewed_at=timezone.now(),
        )
        cycle.version += 1
        cycle.save(update_fields=["version"])
        CycleEvent.objects.create(
            tenant_id=cycle.tenant_id,
            client_id=cycle.client_id,
            cycle=cycle,
            actor=actor,
            type="fiscal.document.reviewed",
            aggregate_type="FiscalDocumentReview",
            aggregate_id=review.pk,
            aggregate_version=review.version,
            payload={
                "document_id": str(fiscal.pk),
                "revision_id": str(revision_id),
                "review_id": str(review.pk),
                "decision": decision,
            },
        )
    return review
