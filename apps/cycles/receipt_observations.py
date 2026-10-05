"""Receipt submissions become payment observations (T17, S6.5, S7.5).

When a client answers a `comprovante` request with a file, three rows tell one
story, and all three are observations rather than statuses:

* the submission itself — `client_submitted` / `pending_review`, written once
  per link (the partial unique on `evidence_link` is the replay floor);
* an acceptance — `staff_recorded` / `accepted`, still an unverified manual
  claim because accepting a file is not checking it against Receita;
* a rejection — `staff_recorded` / `unsupported` / `rejected`, which *supersedes*
  the submission so the file stops counting while its observation stays
  readable in the chain.

`apply_review` writes the second and third inside `record_review`'s locks.
`settle_link` is the materializer's version: it re-takes the same locks in the
same order (request, link, intake — the document row is never touched, so the
scanner's intake→document hold is not contested), decides from the link's
current review state, and is idempotent at every point.
"""

from datetime import date
from typing import Final
from uuid import UUID

import sentry_sdk
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, QuerySet
from django.utils import timezone

from apps.accounts.models import User
from apps.cycles.capabilities import CYCLES_REVIEW
from apps.cycles.evidence import (
    EvidenceLink,
    EvidenceRequest,
    RequestKind,
    ReviewDecision,
)
from apps.cycles.models import DocumentIntake
from apps.cycles.services.cycle import require_actor
from apps.obligations.models import ObservationVerificationStatus, SourceObservation
from apps.obligations.models.observations import ObservationOutcome
from apps.obligations.observations import assert_fact, lock_subjects
from apps.tenants.models import Tenant

RECEIPT_VALUE: Final = "comprovante_recebido"
SUBMISSION_OUTCOME: Final = "submitted"


def _pending_links(queryset: QuerySet[EvidenceLink]) -> QuerySet[EvidenceLink]:
    """Receipt-bound links with no observation citing them yet.

    The partial unique on `(tenant, client, evidence_link)` makes "has an
    observation" equivalent to "materialized", so `Exists` is the whole
    predicate — a decided link whose outcome row is missing is still skipped
    and the review path's own write covers it.
    """
    return queryset.filter(
        request__kind=RequestKind.COMPROVANTE,
        request__obligation__isnull=False,
    ).exclude(Exists(SourceObservation.objects.filter(evidence_link_id=OuterRef("pk"))))


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
    try:
        with transaction.atomic():
            return assert_fact(
                tenant_id=request.tenant_id,
                client_id=request.client_id,
                subject_id=request.obligation_id,
                kind="payment",
                period=_submission_period(request),
                source_kind="client_submitted",
                observed_at=link.created_at,
                value=_submission_value(link),
                verification_status="pending_review",
                outcome=SUBMISSION_OUTCOME,
                evidence_link_id=link.pk,
            )
    except IntegrityError:
        # The partial unique on evidence_link arbitrated a concurrent write:
        # "already materialized" is the same answer, never an error.
        existing = SourceObservation.objects.filter(evidence_link_id=link.pk).first()
        if existing is None:
            raise
        return existing


def _outcome_assertion(
    request: EvidenceRequest, link: EvidenceLink
) -> SourceObservation | None:
    """Find the review outcome row for this link, if a writer already wrote it.

    Decided links carry their outcome as a sibling assertion whose value names
    the link — never through `evidence_link` itself, which the unique reserves
    for the submission. A materializer arriving after the review must not
    write a second acceptance.
    """
    return (
        SourceObservation.objects.filter(
            subject_ref_id=request.obligation_id,
            kind="payment",
            period=_submission_period(request),
            value_json__link_id=str(link.pk),
            outcome__in={"accepted", "rejected"},
        )
        .order_by("observed_at", "pk")
        .last()
    )


def apply_review(
    *,
    request: EvidenceRequest,
    link: EvidenceLink,
    decision: str,
    payment_relevant: bool,
    actor: User | None,
) -> SourceObservation | None:
    """Write the review's observation under `record_review`'s held locks.

    Returns the assertion written, or None when the link is not a bound
    receipt. Acceptance without `payment_relevant` produces nothing: a file the
    firm accepted as the wrong document must not turn into a payment claim.
    """
    if request.kind != RequestKind.COMPROVANTE or request.obligation_id is None:
        return None
    submission = SourceObservation.objects.filter(evidence_link_id=link.pk).first()
    lock_subjects([request.obligation_id])
    if submission is None:
        submission = _write_submission(request=request, link=link)
    prior = _outcome_assertion(request, link)
    if prior is not None:
        return prior
    if decision == ReviewDecision.REJECTED:
        return assert_fact(
            tenant_id=request.tenant_id,
            client_id=request.client_id,
            subject_id=request.obligation_id,
            kind="payment",
            period=_submission_period(request),
            source_kind="staff_recorded",
            observed_at=timezone.now(),
            value=_submission_value(link),
            verification_status="unsupported",
            outcome="rejected",
            recorded_by=actor,
            supersedes=submission,
            evidence_link_id=None,
        )
    if not payment_relevant:
        return None
    return assert_fact(
        tenant_id=request.tenant_id,
        client_id=request.client_id,
        subject_id=request.obligation_id,
        kind="payment",
        period=_submission_period(request),
        source_kind="staff_recorded",
        observed_at=timezone.now(),
        value=_submission_value(link),
        verification_status=ObservationVerificationStatus.UNKNOWN,
        outcome=ObservationOutcome.ACCEPTED,
        recorded_by=actor,
        supersedes=None,
        evidence_link_id=None,
    )


def settle_link(
    link_id: UUID,
    *,
    actor: User | None = None,
) -> SourceObservation | None:
    """Materialize one receipt link's observation; the materializer's unit.

    Lock order mirrors `record_review`: request first, then the link's own row
    (`of=("self",)` — a full lock would take the document row the scanner
    holds), then intake, then the subject. The review decision is re-read
    under the request lock, so a decision that lands mid-materialization is
    folded into the same transaction instead of racing it.
    """
    link = EvidenceLink.objects.filter(pk=link_id).first()
    if link is None:
        return None
    with transaction.atomic():
        request = (
            EvidenceRequest.objects.select_for_update()
            .select_related("cycle")
            .get(pk=link.request_id)
        )
        if request.kind != RequestKind.COMPROVANTE or request.obligation_id is None:
            return None
        link = (
            EvidenceLink.objects.select_for_update(of=("self",))
            .select_related("document")
            .get(pk=link_id)
        )
        # The intake row gates nothing here — the submission observation cites
        # the link, not the scan result — but the scanner may be finalizing the
        # same intake, so we never take a lock it holds in the other order.
        DocumentIntake.objects.select_for_update(of=("self",)).filter(
            document_id=link.document_id
        ).first()
        existing = SourceObservation.objects.filter(evidence_link_id=link.pk).first()
        lock_subjects([request.obligation_id])
        if existing is None:
            submission = _write_submission(request=request, link=link)
            decision = link.review_decision
        else:
            submission = existing
            decision = link.review_decision
        if decision == ReviewDecision.NONE:
            return submission
        review = link.reviews.order_by("-reviewed_at").first()
        if review is None:
            return submission
        # Fold the recorded decision into the same locked transaction; the
        # decision the review service wrote wins over anything the link row
        # happened to show before the locks were taken.
        return apply_review(
            request=request,
            link=link,
            decision=review.decision,
            payment_relevant=review.payment_relevant,
            actor=actor or review.reviewer,
        )


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
