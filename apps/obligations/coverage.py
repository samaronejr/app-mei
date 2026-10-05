"""`coverage_for`: what this firm currently knows about a client's obligations.

T17, S6.5. The answer is deliberately pessimistic in shape: a state name the
interface cannot mistake for more than it is, the claim it comes from, and the
two facets the claim does not cover. A calendar assertion asserts the schedule
exists; it never implies a guide was issued, and a payment claim never implies
a filing.

State vocabulary (one word per item; the badge maps it, never invents it):

* ``verified`` — an official source's current head, still inside its freshness
  window (seven days, the same `OFFICIAL_FRESHNESS` the effect manifest uses).
* ``manual`` — the newest usable head is people-sourced (staff record, accepted
  client submission, or a legacy flag restated at T9). Informed, not verified.
* ``pending_review`` — the newest usable head is a client submission the firm
  has not decided on yet.
* ``stale`` — the newest usable head is official but outside its window.
* ``refresh_failed`` — the newest usable head is an official refresh failure;
  the last good result is still surfaced separately, never implied current.
* ``unknown`` — nothing usable: no obligation row, no claims at all, or the
  newest usable head asserts the claim is unsupported. Also the answer when
  the client's tax timeline has a gap at this competence — a profile gap is a
  hole in knowledge, not proof of inapplicability.
* ``not_applicable`` — the engagement is active and the client's regime at this
  competence makes the mapped obligation not owed. A positive answer, only
  where one can be had.
* ``unknown_scope`` — the service kind has no obligation mapping at all. The
  row renders inert: nothing here claims either applicability or coverage.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Final
from uuid import UUID

from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import User
from apps.clients.models import ClientCompany, Engagement, TaxProfile, TaxRegime
from apps.clients.services import profile_at
from apps.core.flags import tenant_has
from apps.obligations.models import (
    Obligation,
    ObservationVerificationStatus,
    SourceObservation,
)
from apps.obligations.presentation import MANUAL_SOURCES, OFFICIAL_SOURCES

# An official verification stops being fresh after seven days — the manifest
# uses the same bound, and two different numbers would make "verified"
# mean different things on different screens.
OFFICIAL_FRESHNESS: Final = timedelta(days=7)

# Which obligation types an engagement's coverage answers about. A service kind
# outside the map is not "covered" and not "not applicable": its scope was never
# defined, and inventing one would be a silent product decision.
SERVICE_OBLIGATIONS: Final = {
    "mei_mensal": ("DAS",),
    "documentos_mensal": ("documentos",),
}

# The facets an item reports independently. `payment` answers "was it paid";
# `guide` answers "was a guide issued"; `calendar` answers "was the schedule
# asserted by the deterministic generator". None implies another.
FACET_KINDS: Final = {
    "payment": "payment",
    "guide": "guide_issued",
    "calendar": "calendar_scheduled",
}


class CoverageState:
    """The coverage vocabulary the surfaces may render."""

    VERIFIED: Final = "verified"
    MANUAL: Final = "manual"
    PENDING_REVIEW: Final = "pending_review"
    STALE: Final = "stale"
    REFRESH_FAILED: Final = "refresh_failed"
    UNKNOWN: Final = "unknown"
    NOT_APPLICABLE: Final = "not_applicable"
    UNKNOWN_SCOPE: Final = "unknown_scope"


@dataclass(frozen=True)
class CoverageItem:
    """One engagement's coverage answer for one competence."""

    engagement_id: UUID
    service_kind: str
    state: str
    obligation_id: UUID | None
    facets: dict[str, str] = field(default_factory=dict)
    last_success: datetime | None = None
    head_id: UUID | None = None
    head_source: str = ""
    head_verification: str = ""

    @property
    def correctable(self) -> bool:
        """Whether the driving head is a manual claim a staff correction names."""
        return self.head_source in MANUAL_SOURCES and self.state in {
            CoverageState.MANUAL,
            CoverageState.PENDING_REVIEW,
        }


def _heads(rows: list[SourceObservation]) -> list[SourceObservation]:
    superseded = {row.supersedes_id for row in rows if row.supersedes_id}
    return [row for row in rows if row.pk not in superseded]


def _usable(head: SourceObservation) -> bool:
    """Return whether a head still asserts something a reader can rely on."""
    return head.outcome not in {"rejected", "retracted"} and (
        head.verification_status
        not in {
            ObservationVerificationStatus.UNSUPPORTED,
            ObservationVerificationStatus.NOT_APPLICABLE,
        }
    )


def _facet_state(  # noqa: PLR0911 - each source kind answers differently
    head: SourceObservation | None, now: datetime
) -> str:
    if head is None or not _usable(head):
        return CoverageState.UNKNOWN
    if head.source_kind == "deterministic_rule":
        # The deterministic producer is the authority on the schedule it wrote
        # — and on nothing else. A rule row asserting payment or issuance can
        # never borrow the verified badge.
        return (
            CoverageState.VERIFIED
            if head.kind == "calendar_scheduled"
            and head.verification_status == "verified"
            else CoverageState.UNKNOWN
        )
    if head.source_kind in MANUAL_SOURCES:
        return (
            CoverageState.PENDING_REVIEW
            if head.verification_status == "pending_review"
            else CoverageState.MANUAL
        )
    if head.source_kind in OFFICIAL_SOURCES:
        if head.verification_status == "refresh_failed":
            return CoverageState.REFRESH_FAILED
        if head.verification_status == "verified":
            return (
                CoverageState.VERIFIED
                if head.observed_at + OFFICIAL_FRESHNESS >= now
                else CoverageState.STALE
            )
        if head.verification_status == "stale":
            return CoverageState.STALE
    # deterministic_rule and any future source land here: the row exists, the
    # mapping has no honest label for it, and borrowing a manual badge would be
    # the verified-style lie the render policy exists to prevent.
    return CoverageState.UNKNOWN


def _engagement_state(
    engagement: Engagement,
    profile: TaxProfile | None,
    obligations: dict[str, Obligation],
    rows: list[SourceObservation],
    now: datetime,
) -> CoverageItem:
    codes = SERVICE_OBLIGATIONS.get(engagement.service_kind)
    if codes is None:
        return CoverageItem(
            engagement_id=engagement.pk,
            service_kind=engagement.service_kind,
            state=CoverageState.UNKNOWN_SCOPE,
            obligation_id=None,
        )
    if profile is None or (codes == ("DAS",) and profile.regime == TaxRegime.UNKNOWN):
        # A timeline gap AND a positively-unknown regime are the same answer:
        # the system cannot say whether a DAS was owed, and "not applicable"
        # would claim it could.
        return CoverageItem(
            engagement_id=engagement.pk,
            service_kind=engagement.service_kind,
            state=CoverageState.UNKNOWN,
            obligation_id=None,
        )
    if codes == ("DAS",) and profile.regime != TaxRegime.MEI:
        return CoverageItem(
            engagement_id=engagement.pk,
            service_kind=engagement.service_kind,
            state=CoverageState.NOT_APPLICABLE,
            obligation_id=None,
        )
    subject = next(
        (obligation for code in codes if (obligation := obligations.get(code))),
        None,
    )
    if subject is None:
        return CoverageItem(
            engagement_id=engagement.pk,
            service_kind=engagement.service_kind,
            state=CoverageState.UNKNOWN,
            obligation_id=None,
        )
    subject_heads = _heads([row for row in rows if row.subject_ref_id == subject.pk])

    def driving_head(kind: str) -> SourceObservation | None:
        """Newest usable head of one claim chain, else its newest head at all."""
        chain = [row for row in subject_heads if row.kind == kind]
        usable = [row for row in chain if _usable(row)]
        if usable:
            return usable[-1]
        return chain[-1] if chain else None

    facets = {
        name: _facet_state(driving_head(kind), now)
        for name, kind in FACET_KINDS.items()
    }
    head = driving_head(FACET_KINDS["payment"])
    return CoverageItem(
        engagement_id=engagement.pk,
        service_kind=engagement.service_kind,
        state=_facet_state(head, now),
        obligation_id=subject.pk,
        facets=facets,
        last_success=head.observed_at if head is not None else None,
        head_id=head.pk if head is not None else None,
        head_source=head.source_kind if head is not None else "",
        head_verification=head.verification_status if head is not None else "",
    )


def coverage_for(
    actor: User,  # noqa: ARG001 - scope is the caller's provenance, not a filter
    client: ClientCompany,
    period: date,
    *,
    engagement_id: UUID | None = None,
    now: datetime | None = None,
) -> list[CoverageItem]:
    """Return the coverage items for one client's one competence.

    Read-only: nothing here writes, and the clock is a parameter so the same
    data answers differently at different evaluation times — a test pins `now`,
    a page leaves it unset. `actor` stays in the signature so the call reads
    like the other workspace services and a future scoping hook has a home.
    """
    if not tenant_has(client.tenant_id, "cycles_r1"):
        return []
    moment = now or timezone.now()
    engagements = (
        Engagement.objects.filter(client_id=client.pk, starts_on__lte=period)
        .filter(Q(ends_on__isnull=True) | Q(ends_on__gt=period))
        .order_by("pk")
    )
    if engagement_id is not None:
        engagements = engagements.filter(pk=engagement_id)
    engagement_rows: list[Engagement] = list(engagements)
    if not engagement_rows:
        return []
    profile = profile_at(client, period)
    obligations = {
        row.obligation_type_id: row
        for row in Obligation.objects.filter(
            client_id=client.pk, competence_month=period
        )
    }
    rows = list(
        SourceObservation.objects.filter(
            client_id=client.pk,
            subject_ref_id__in=[row.pk for row in obligations.values()],
            kind__in=set(FACET_KINDS.values()),
        )
        .filter(Q(period=period) | Q(period__isnull=True))
        .order_by("observed_at", "pk")
    )
    return [
        _engagement_state(engagement, profile, obligations, rows, moment)
        for engagement in engagement_rows
    ]
