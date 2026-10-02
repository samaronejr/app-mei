"""`record_review` (S7.5): accept or reject one submitted file, with field provenance.

The command follows the firm-side contract (S6.3): the cycle is loaded through the
portfolio scope (an unassigned staff accountant gets 404 with nothing written), the
cycle and the request rows are locked, the request `version` the page was rendered
with is checked (`VersionConflict`, 409, before any write), `can(cycles.review)` is
re-run inside the lock and one `CycleEvent` is written.

What it writes: a `Review` (firm-only), one `staff` `ExtractedField` per correction
(the suggestion stays beside it), the client-visible projection on the link
(decision, pt-BR reason, time), the request's state, and -- on an accepted clean
file -- the intake's `vinculado`. It never writes into the `Document` or its bytes,
and it never moves a document to another client.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.utils import timezone

from apps.accounts.models import User
from apps.cycles.capabilities import CYCLES_REVIEW
from apps.cycles.errors import IllegalTransition
from apps.cycles.evidence import (
    CLIENT_REASON_MAX_LENGTH,
    REVIEWABLE_STATES,
    DocumentIntake,
    EvidenceLink,
    EvidenceRequest,
    ExtractedField,
    FieldOrigin,
    IntakeState,
    RequestState,
    Review,
    ReviewDecision,
)
from apps.cycles.extraction import FIELD_LABELS, is_cnpj_field
from apps.cycles.records import CycleEvent
from apps.cycles.services.cycle import (
    OPEN_STATES,
    check_version,
    load_cycle,
    require_actor,
)
from apps.fiscal.validators import normalize_document, validate_cnpj

VALUE_MAX: Final = 255
OVERRIDE_MAX: Final = 2000
CLOSED_REQUESTS: Final = frozenset(
    {RequestState.DRAFT, RequestState.WAIVED, RequestState.REPLACED}
)


@dataclass(frozen=True, slots=True)
class FieldCorrection:
    """One reviewed value beside the suggestion it replaces (S7.5)."""

    field: str
    suggested_value: str
    reviewed_value: str
    suggested_origin: str


@dataclass(frozen=True, slots=True)
class ReviewRef:
    """The recorded decision and the request revision it produced."""

    id: UUID
    decision: str
    request_version: int


def _clean_corrections(corrections: list[FieldCorrection]) -> list[FieldCorrection]:
    cleaned: list[FieldCorrection] = []
    seen: set[str] = set()
    for correction in corrections:
        value = correction.reviewed_value.strip()
        if correction.field not in FIELD_LABELS or correction.field in seen:
            msg = "Campo de revisão desconhecido."
            raise ValidationError(msg)
        seen.add(correction.field)
        if len(value) > VALUE_MAX:
            msg = "Os valores revisados aceitam até 255 caracteres."
            raise ValidationError(msg)
        if is_cnpj_field(correction.field):
            value = normalize_document(value)
            try:
                validate_cnpj(value)
            except ValidationError as error:
                label = FIELD_LABELS[correction.field]
                msg = f"{label}: CNPJ inválido."
                raise ValidationError(msg) from error
        if value and value != correction.suggested_value:
            cleaned.append(
                FieldCorrection(
                    correction.field,
                    correction.suggested_value,
                    value,
                    correction.suggested_origin,
                )
            )
    return cleaned


def _validate(
    decision: str, client_reason: str, override_reason: str
) -> tuple[str, str]:
    if decision not in {ReviewDecision.ACCEPTED, ReviewDecision.REJECTED}:
        msg = "Escolha aceitar ou não aceitar o arquivo."
        raise ValidationError(msg)
    reason = client_reason.strip()
    if decision == ReviewDecision.REJECTED and not reason:
        msg = "Explique ao cliente por que o arquivo não foi aceito."
        raise ValidationError(msg)
    if len(reason) > CLIENT_REASON_MAX_LENGTH:
        msg = "O motivo para o cliente aceita até 500 caracteres."
        raise ValidationError(msg)
    override = override_reason.strip()
    if len(override) > OVERRIDE_MAX:
        msg = "A justificativa interna aceita até 2.000 caracteres."
        raise ValidationError(msg)
    return reason, override


def _save_corrections(
    link: EvidenceLink,
    actor: User,
    corrections: list[FieldCorrection],
    now: datetime,
) -> None:
    ExtractedField.objects.bulk_create(
        ExtractedField(
            tenant_id=link.tenant_id,
            client_id=link.client_id,
            document_id=link.document_id,
            name=correction.field,
            value=correction.reviewed_value,
            origin=FieldOrigin.STAFF,
            recorded_by=actor,
            created_at=now,
        )
        for correction in corrections
    )


def record_review(  # noqa: PLR0913 - the S7.5 contract names every argument
    actor: User,
    link_id: UUID,
    *,
    decision: str,
    client_reason: str,
    corrections: list[FieldCorrection],
    expected_version: int,
    third_party: bool = False,
    override_reason: str = "",
) -> ReviewRef:
    """Decide on one submitted file against the request revision the page showed."""
    reason, override = _validate(decision, client_reason, override_reason)
    cleaned = _clean_corrections(corrections)
    with transaction.atomic():
        candidate = EvidenceLink.objects.filter(pk=link_id).first()
        if candidate is None:
            raise Http404
        cycle_id = (
            EvidenceRequest.objects.filter(pk=candidate.request_id)
            .values_list("cycle_id", flat=True)
            .get()
        )
        cycle = load_cycle(actor, cycle_id, lock=True)
        request = EvidenceRequest.objects.select_for_update().get(
            pk=candidate.request_id
        )
        check_version(request.version, expected_version)
        require_actor(actor, CYCLES_REVIEW)
        if cycle.state not in OPEN_STATES or request.state in CLOSED_REQUESTS:
            msg = "Esta solicitação não está aberta para revisão."
            raise IllegalTransition(msg)
        link = (
            EvidenceLink.objects.select_for_update()
            .select_related("document")
            .get(pk=candidate.pk)
        )
        if link.review_decision:
            msg = "Este arquivo já foi revisado."
            raise IllegalTransition(msg)
        intake = (
            DocumentIntake.objects.select_for_update()
            .filter(document_id=link.document_id)
            .first()
        )
        if intake is not None and intake.state not in REVIEWABLE_STATES:
            msg = "Aguarde a verificação e a leitura automática do arquivo."
            raise IllegalTransition(msg)
        accepting = decision == ReviewDecision.ACCEPTED
        is_divergent = intake is not None and intake.state == IntakeState.DIVERGENT
        if accepting and is_divergent and not (third_party and override):
            msg = (
                "O CNPJ lido neste arquivo não é o deste cliente. Para aceitar, "
                "marque 'Documento de terceiro relevante' e registre a justificativa."
            )
            raise ValidationError(msg)
        now = timezone.now()
        review = Review.objects.create(
            tenant_id=link.tenant_id,
            client_id=link.client_id,
            link=link,
            decision=decision,
            client_reason=reason,
            third_party=accepting and is_divergent and third_party,
            override_reason=override if accepting and is_divergent else "",
            corrections_json=[
                {
                    "field": correction.field,
                    "suggested_value": correction.suggested_value,
                    "reviewed_value": correction.reviewed_value,
                    "suggested_origin": correction.suggested_origin,
                }
                for correction in cleaned
            ],
            reviewer=actor,
            reviewed_at=now,
            document_sha256=link.document.sha256,
        )
        _save_corrections(link, actor, cleaned, now)
        link.review_decision = decision
        link.client_reason = reason
        link.reviewed_at = now
        link.version += 1
        link.save(
            update_fields=["review_decision", "client_reason", "reviewed_at", "version"]
        )
        previous = request.state
        if accepting:
            request.state = RequestState.ACCEPTED
        elif request.state != RequestState.ACCEPTED:
            request.state = RequestState.REJECTED
        request.version += 1
        request.save(update_fields=["state", "version"])
        if (
            accepting
            and intake is not None
            and intake.state
            in {
                IntakeState.CLASSIFIED,
                IntakeState.DIVERGENT,
            }
        ):
            intake.state = IntakeState.LINKED
            intake.version += 1
            intake.save(update_fields=["state", "version"])
        CycleEvent.objects.create(
            tenant_id=cycle.tenant_id,
            client_id=cycle.client_id,
            cycle=cycle,
            actor=actor,
            type="document.reviewed",
            aggregate_type="EvidenceRequest",
            aggregate_id=request.pk,
            aggregate_version=request.version,
            payload={
                "link_id": str(link.pk),
                "document_id": str(link.document_id),
                "review_id": str(review.pk),
                "decision": decision,
                "from": previous,
                "to": request.state,
                "corrections": len(cleaned),
            },
        )
        return ReviewRef(review.pk, decision, request.version)


__all__ = ["FieldCorrection", "ReviewRef", "record_review"]
