"""`coverage_for`: what this firm currently knows about a client's obligations.

T17, S6.5. The answer is deliberately pessimistic in shape: a state name the
interface cannot mistake for more than it is, the claim it comes from, and the
two facets the claim does not cover. A calendar assertion asserts the schedule
exists; it never implies a guide was issued, and a payment claim never implies
a filing.

State vocabulary (one word per item; the badge maps it, never invents it):

* ``verified`` — an official source's current head, still inside its freshness
  window (seven days for official_api; file imports have no implicit expiry).
* ``manual`` — the selected usable head is people-sourced (staff record, accepted
  client submission, or a legacy flag restated at T9). Informed, not verified.
* ``pending_review`` — the newest usable head is a client submission the firm
  has not decided on yet.
* ``stale`` — the newest usable head is official but outside its window.
* ``refresh_failed`` — only a failed attempt remains; a separate good claim
  would retain its own source, state and successful observation time.
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

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Final
from uuid import UUID

from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import User
from apps.authz.objects import load_visible_client
from apps.clients.models import ClientCompany, Engagement, TaxProfile, TaxRegime
from apps.clients.services import claim_at
from apps.core.flags import tenant_has
from apps.core.tenancy import MissingTenantContext, current_tenant_id
from apps.obligations.models import (
    Obligation,
    ObservationSubjectKind,
    SourceObservation,
)
from apps.obligations.presentation import (
    MANUAL_SOURCES,
    current_observation,
    payment_state,
)

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
    covered_by: dict[str, SourceObservation] = field(default_factory=dict)

    @property
    def correctable(self) -> bool:
        """Whether the driving head is a manual claim a staff correction names."""
        return self.head_source in MANUAL_SOURCES and self.state in {
            CoverageState.MANUAL,
            CoverageState.PENDING_REVIEW,
            CoverageState.STALE,
        }


@dataclass(frozen=True, slots=True)
class CoverageInputs:
    """Already scoped rows; this value grants no client visibility."""

    engagements: list[Engagement]
    profiles: list[TaxProfile]
    obligations: list[Obligation]
    observations: list[SourceObservation]


def coverage_from_rows(
    rows: CoverageInputs, period: date, *, now: datetime
) -> list[CoverageItem]:
    """Evaluate the same coverage rules without fetching their inputs again."""
    profile = claim_at(rows.profiles, period)
    obligations = {row.obligation_type_id: row for row in rows.obligations}
    return [
        _engagement_state(engagement, profile, obligations, rows.observations, now)
        for engagement in rows.engagements
    ]


def _facet_state(head: SourceObservation | None, now: datetime) -> str:
    if head is None:
        return CoverageState.UNKNOWN
    state = payment_state(
        head.source_kind,
        head.verification_status,
        head.outcome,
        observed_at=head.observed_at,
        now=now,
    )
    if state == CoverageState.MANUAL and head.verification_status == "pending_review":
        return CoverageState.PENDING_REVIEW
    if state == CoverageState.STALE and (
        head.verification_status == "refresh_failed" or head.outcome == "refresh_failed"
    ):
        return CoverageState.REFRESH_FAILED
    return state


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
    covered_by = {
        name: head
        for name, kind in FACET_KINDS.items()
        if (
            head := current_observation(
                rows,
                subject_id=subject.pk,
                period=subject.competence_month,
                kind=kind,
            )
        )
        is not None
    }
    facets = {name: _facet_state(covered_by.get(name), now) for name in FACET_KINDS}
    head = covered_by.get("payment")
    state = facets["payment"]
    return CoverageItem(
        engagement_id=engagement.pk,
        service_kind=engagement.service_kind,
        state=state,
        obligation_id=subject.pk,
        facets=facets,
        last_success=(
            head.observed_at
            if head is not None
            and state
            in {CoverageState.VERIFIED, CoverageState.MANUAL, CoverageState.STALE}
            else None
        ),
        head_id=head.pk if head is not None else None,
        head_source=head.source_kind if head is not None else "",
        head_verification=head.verification_status if head is not None else "",
        covered_by=covered_by,
    )


def coverage_for(
    actor: User,
    client: ClientCompany,
    period: date,
    *,
    engagement_id: UUID | None = None,
    now: datetime | None = None,
) -> list[CoverageItem]:
    """Return the coverage items for one client's one competence.

    Read-only: nothing here writes, and the clock is a parameter so the same
    data answers differently at different evaluation times — a test pins `now`,
    a page leaves it unset. Visibility is checked here as well as by the route:
    another caller cannot read an unassigned client's coverage.
    """
    client = load_visible_client(actor, client.pk, tenant_id=client.tenant_id)
    items = coverage_for_clients([client], period, now=now)[client.pk]
    return (
        items
        if engagement_id is None
        else [item for item in items if item.engagement_id == engagement_id]
    )


def coverage_for_clients(
    clients: Iterable[ClientCompany],
    period: date,
    *,
    now: datetime | None = None,
) -> dict[UUID, list[CoverageItem]]:
    """Read already portfolio-scoped clients in the current tenant, in four batches.

    Callers supply visible_clients-scoped rows. Every data query additionally
    constrains both the tenant and the supplied client IDs; no portfolio is
    expanded here. Empty clients still receive an empty coverage list.
    The entitlement read is cached by firm requests, or costs one extra query
    outside them. Neither data nor entitlement query count grows with clients.
    """
    answers, _engagements = coverage_and_engagements_for_clients(
        clients, period, now=now
    )
    return answers


def coverage_and_engagements_for_clients(
    clients: Iterable[ClientCompany],
    period: date,
    *,
    now: datetime | None = None,
) -> tuple[dict[UUID, list[CoverageItem]], dict[UUID, Engagement]]:
    """Read the same batch, also returning its engagement rows by primary key.

    Each engagement already carries its responsible user, so a caller that labels
    the answers needs no second engagement read.
    """
    answers: dict[UUID, list[CoverageItem]] = {client.pk: [] for client in clients}
    loaded: dict[UUID, Engagement] = {}
    if not answers:
        return answers, loaded
    tenant = current_tenant_id.get()
    if tenant is None:
        msg = "Coverage requires an established tenant context."
        raise MissingTenantContext(msg)
    if not tenant_has(tenant, "cycles_r1"):
        return answers, loaded
    client_ids = tuple(answers)
    moment = now or timezone.now()
    engagements: dict[UUID, list[Engagement]] = {}
    for engagement in (
        Engagement.objects.filter(
            tenant_id=tenant, client_id__in=client_ids, starts_on__lte=period
        )
        .filter(Q(ends_on__isnull=True) | Q(ends_on__gt=period))
        .select_related("responsible")
        .order_by("pk")
    ):
        engagements.setdefault(engagement.client_id, []).append(engagement)
        loaded[engagement.pk] = engagement
    profiles: dict[UUID, list[TaxProfile]] = {}
    for profile_row in TaxProfile.objects.filter(
        tenant_id=tenant, client_id__in=client_ids
    ):
        profiles.setdefault(profile_row.client_id, []).append(profile_row)
    obligations: dict[UUID, dict[str, Obligation]] = {}
    for subject in Obligation.objects.filter(
        tenant_id=tenant, client_id__in=client_ids, competence_month=period
    ):
        obligations.setdefault(subject.client_id, {})[subject.obligation_type_id] = (
            subject
        )
    observations: dict[UUID, list[SourceObservation]] = {}
    for row in (
        SourceObservation.objects.filter(
            tenant_id=tenant,
            client_id__in=client_ids,
            subject_kind=ObservationSubjectKind.OBLIGATION,
            subject_ref__competence_month=period,
            kind__in=set(FACET_KINDS.values()),
        )
        .filter(Q(period=period) | Q(period__isnull=True))
        .order_by("observed_at", "pk")
    ):
        observations.setdefault(row.client_id, []).append(row)
    for client_id in client_ids:
        answers[client_id] = coverage_from_rows(
            CoverageInputs(
                engagements.get(client_id, []),
                profiles.get(client_id, []),
                list(obligations.get(client_id, {}).values()),
                observations.get(client_id, []),
            ),
            period,
            now=moment,
        )
    return answers, loaded
