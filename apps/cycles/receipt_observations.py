"""Receipt submissions become payment observations (T17, S6.5, S7.5).

When a client answers a `comprovante` request with a file, three rows tell one
story, and all three are observations rather than statuses:

* the submission itself — `client_submitted` / `pending_review`, written once
  per link (the partial unique on `evidence_link` is the replay floor);
* an acceptance — a `staff_recorded` successor of that exact submission,
  still an unverified manual claim, with the real Review/link provenance;
* a rejection — `staff_recorded` / `unsupported` / `rejected`, which *supersedes*
  the submission so the file stops counting while its observation stays
  readable in the chain.

`apply_review` writes the second and third inside `record_review`'s locks.
`settle_link` takes client, cycle, request, link and then subject locks.
Submission materialization never locks intake or document: finalizers hold
those before acquiring the cycle, and a submission asserts only the link.
"""

from datetime import date
from typing import Final
from uuid import UUID

import sentry_sdk
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Exists, OuterRef, QuerySet

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.flags import tenant_has
from apps.cycles.capabilities import CYCLES_REVIEW
from apps.cycles.errors import VersionConflict
from apps.cycles.evidence import (
    DocumentIntake,
    EvidenceLink,
    EvidenceRequest,
    IntakeState,
    RequestKind,
    Review,
    ReviewDecision,
    ScanVerdict,
)
from apps.cycles.extraction import DIVERGENT
from apps.cycles.models import ServiceCycle
from apps.cycles.services.cycle import require_actor, tenant_id
from apps.obligations.models import (
    ObservationKind,
    ObservationSourceKind,
    ObservationVerificationStatus,
    SourceObservation,
)
from apps.obligations.models.observations import ObservationOutcome
from apps.obligations.observations import assert_fact, lock_subjects
from apps.tenants.models import Tenant

RECEIPT_VALUE: Final = "comprovante_recebido"
SUBMISSION_OUTCOME: Final = "submitted"


def _pending_links(queryset: QuerySet[EvidenceLink]) -> QuerySet[EvidenceLink]:
    """Advisory inventory; the subject-fenced unit repeats submission dedupe."""
    return queryset.filter(
        request__kind=RequestKind.COMPROVANTE,
        request__obligation__isnull=False,
    ).exclude(
        Exists(
            SourceObservation.objects.filter(
                evidence_link_id=OuterRef("pk"),
                kind=ObservationKind.PAYMENT,
                source_kind=ObservationSourceKind.CLIENT_SUBMITTED,
            )
        )
    )


def _submission_period(request: EvidenceRequest) -> date:
    """Return the competence the receipt claims to pay for — the cycle's."""
    return request.cycle.period_start


def _submission_value(link: EvidenceLink) -> dict[str, object]:
    return {
        "document_kind": "comprovante",
        "link_id": str(link.pk),
        "sha256": link.document.sha256,
    }


def _write_submission(
    *,
    request: EvidenceRequest,
    link: EvidenceLink,
) -> SourceObservation:
    # A comprovante request that reached a submission cannot lack a binding.
    if request.obligation_id is None:  # pragma: no cover - binding is enforced upstream
        msg = "Receipt request missing obligation binding."
        raise RuntimeError(msg)
    return assert_fact(
        tenant_id=request.tenant_id,
        client_id=request.client_id,
        subject_id=request.obligation_id,
        kind=ObservationKind.PAYMENT,
        period=_submission_period(request),
        source_kind=ObservationSourceKind.CLIENT_SUBMITTED,
        observed_at=link.created_at,
        value=_submission_value(link),
        verification_status=ObservationVerificationStatus.PENDING_REVIEW,
        outcome=SUBMISSION_OUTCOME,
        recorded_by=link.submitted_by,
        evidence_link_id=link.pk,
    )


def _submission(request: EvidenceRequest, link: EvidenceLink) -> SourceObservation:
    """Lock the actual scoped subject before binding checks and dedupe."""
    if request.obligation_id is None:
        msg = "Vincule a obrigação deste comprovante."
        raise ValidationError(msg)
    subjects = lock_subjects([request.obligation_id], client_id=request.client_id)
    if not subjects or subjects[0].competence_month != _submission_period(request):
        msg = "O comprovante não pertence à obrigação desta competência."
        raise ValidationError(msg)
    existing = SourceObservation.objects.filter(
        client_id=request.client_id,
        subject_ref_id=request.obligation_id,
        kind=ObservationKind.PAYMENT,
        period=_submission_period(request),
        source_kind=ObservationSourceKind.CLIENT_SUBMITTED,
        evidence_link_id=link.pk,
    ).first()
    return existing or _write_submission(request=request, link=link)


def payment_evidence_eligible(
    request: EvidenceRequest, link: EvidenceLink, intake: DocumentIntake | None
) -> bool:
    """Document acceptance alone never qualifies a receipt as payment evidence."""
    return bool(
        request.kind == RequestKind.COMPROVANTE
        and request.obligation_id is not None
        and link.request_id == request.pk
        and link.client_id == request.client_id
        and intake is not None
        and intake.document_id == link.document_id
        and intake.client_id == request.client_id
        and intake.verdict == ScanVerdict.CLEAN
        and intake.state in {IntakeState.CLASSIFIED, IntakeState.LINKED}
        # A third-party acceptance changes state to LINKED, not provenance.
        and intake.reason != DIVERGENT
    )


def _outcome_assertion(
    request: EvidenceRequest, link: EvidenceLink, review: Review
) -> SourceObservation | None:
    """Replay the actual Review identity, not a portal-visible decision flag."""
    return (
        SourceObservation.objects.filter(
            subject_ref_id=request.obligation_id,
            client_id=request.client_id,
            kind=ObservationKind.PAYMENT,
            period=_submission_period(request),
            evidence_link_id=link.pk,
            value_json__review_id=str(review.pk),
        )
        .order_by("observed_at", "pk")
        .last()
    )


def apply_review(
    *,
    request: EvidenceRequest,
    link: EvidenceLink,
    review: Review,
    intake: DocumentIntake | None,
) -> SourceObservation | None:
    """Write the review's observation under `record_review`'s held locks.

    Returns the assertion written, or None when the link is not a bound
    receipt. Acceptance without `payment_relevant` produces nothing: a file the
    firm accepted as the wrong document must not turn into a payment claim.
    """
    if request.kind != RequestKind.COMPROVANTE or request.obligation_id is None:
        return None
    if review.link_id != link.pk or review.document_sha256 != link.document.sha256:
        msg = "A revisão não corresponde ao comprovante selecionado."
        raise ValidationError(msg)
    submission = _submission(request, link)
    require_review_actor(review.reviewer)
    prior = _outcome_assertion(request, link, review)
    if prior is not None:
        return prior
    rejecting = review.decision == ReviewDecision.REJECTED
    if not rejecting and not (
        review.decision == ReviewDecision.ACCEPTED
        and review.payment_relevant
        and not review.third_party
        and payment_evidence_eligible(request, link, intake)
    ):
        return None
    if SourceObservation.objects.filter(
        supersedes=submission,
        subject_ref_id=request.obligation_id,
        kind=ObservationKind.PAYMENT,
        period=submission.period,
    ).exists():
        msg = "O registro deste comprovante mudou. Recarregue antes de revisar."
        raise VersionConflict(msg)
    return assert_fact(
        tenant_id=request.tenant_id,
        client_id=request.client_id,
        subject_id=request.obligation_id,
        kind=ObservationKind.PAYMENT,
        period=_submission_period(request),
        source_kind=ObservationSourceKind.STAFF_RECORDED,
        observed_at=review.reviewed_at,
        value={
            **_submission_value(link),
            "review_id": str(review.pk),
            "submission_id": str(submission.pk),
        },
        verification_status=(
            ObservationVerificationStatus.UNSUPPORTED
            if rejecting
            else ObservationVerificationStatus.UNKNOWN
        ),
        outcome=(
            ObservationOutcome.REJECTED if rejecting else ObservationOutcome.ACCEPTED
        ),
        recorded_by=review.reviewer,
        supersedes=submission,
        evidence_link_id=link.pk,
    )


def settle_link(link_id: UUID) -> SourceObservation | None:
    """Materialize one receipt link's observation; the materializer's unit.

    Lock each outer aggregate explicitly, never through an unordered joined
    locking SELECT. Intake/document are read-only here, just as they are in
    T16 source revalidation; scan/extraction finalization owns their locks.
    """
    if not tenant_has(tenant_id(), "cycles_r1"):
        return None
    link = EvidenceLink.objects.filter(pk=link_id).first()
    if link is None:
        return None
    with transaction.atomic():
        ClientCompany.objects.select_for_update(no_key=True).get(pk=link.client_id)
        cycle = ServiceCycle.objects.select_for_update(no_key=True).get(
            pk=link.request.cycle_id, client_id=link.client_id
        )
        request = EvidenceRequest.objects.select_for_update(
            no_key=True, of=("self",)
        ).get(pk=link.request_id, client_id=link.client_id, cycle=cycle)
        request.cycle = cycle
        if request.kind != RequestKind.COMPROVANTE or request.obligation_id is None:
            return None
        link = (
            EvidenceLink.objects.select_for_update(no_key=True, of=("self",))
            .select_related("document")
            .get(pk=link_id)
        )
        # Review commits the submission and its qualified successor atomically.
        # A late job only finds that original submission; it cannot reinterpret
        # a later intake state or a forged portal decision as a staff Review.
        return _submission(request, link)


def settle_pending(tenant: Tenant, *, limit: int = 200) -> int:
    """Materialize every pending receipt link of one tenant; returns count."""
    written = 0
    for link in _pending_links(EvidenceLink.objects.filter(tenant_id=tenant.pk))[
        :limit
    ]:
        try:
            if settle_link(link.pk) is not None:
                written += 1
        except Exception as error:  # noqa: BLE001 - one bad link never stops a sweep
            sentry_sdk.capture_exception(error)
    return written


def require_review_actor(actor: User) -> None:
    """Re-evaluate the review capability for callers that hold locks already."""
    require_actor(actor, CYCLES_REVIEW)
