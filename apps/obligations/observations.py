"""The write path for source observations (T17, S6.5).

Three entry points, all converging on `assert_fact` under the subject lock:

* `record_staff_fact` — a staff member records (or corrects, or retracts) a
  manual fact: the staff capability is re-evaluated under the lock, the head is
  re-read, and the write never updates, only supersedes.
* `emit_calendar_assertions` — the deterministic generator's producer: each new
  obligation's stored schedule is asserted as `calendar_scheduled`. Identical
  replays return the existing row; a corrected schedule supersedes the stale
  head. The fact asserts the schedule exists -- never that a guide was issued
  or money moved.
* `assert_fact` — the raw insert used by the receipt services in
  `apps.cycles.receipt_observations`, which hold the request and subject locks
  already.

Lock discipline: the mutable subject is locked `NO KEY UPDATE`, like the
T15 source reader and INSERT fence. The explicit lock must precede the head
read: a trigger that locks only at INSERT cannot protect an earlier decision.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.utils import timezone

from apps.accounts.models import User
from apps.authz.approvals import lock_authorities
from apps.authz.portfolio import visible_clients
from apps.clients.maintenance import load_client
from apps.clients.models import ClientAssignment, ClientCompany, Engagement, TaxRegime
from apps.clients.services import profile_at
from apps.cycles.capabilities import CYCLES_OPERATE
from apps.cycles.errors import IllegalTransition
from apps.cycles.evidence import EvidenceLink, EvidenceRequest
from apps.cycles.models import ServiceCycle
from apps.cycles.services.cycle import (
    OPEN_STATES,
    check_version,
    emit,
    load_cycle,
    require_actor,
    require_enabled,
    tenant_id,
)
from apps.obligations.coverage import SERVICE_OBLIGATIONS
from apps.obligations.models import (
    Obligation,
    ObservationKind,
    ObservationVerificationStatus,
    SourceObservation,
)

SHA256_RE: Final = re.compile(r"\A[0-9a-f]{64}\Z")

# Outcome codes written by this service. `accepted`/`rejected`/`submitted` belong
# to the receipt materializer (`apps.cycles.receipt_observations`); `recorded`,
# `corrected` and `retracted` are the staff-fact path; `scheduled` is the
# calendar producer.
OUTCOME_RECORDED: Final = "recorded"
OUTCOME_CORRECTED: Final = "corrected"
OUTCOME_RETRACTED: Final = "retracted"
OUTCOME_SCHEDULED: Final = "scheduled"

SOURCE_DETERMINISTIC_RULE: Final = "deterministic_rule"
SOURCE_STAFF_RECORDED: Final = "staff_recorded"


class StaleObservationError(ValidationError):
    """The expected chain head moved since the caller's snapshot."""


def lock_subjects(
    subject_ids: list[UUID], *, client_id: UUID | None = None
) -> list[Obligation]:
    """Take the observation subject locks in id order, NO KEY UPDATE.

    Returns the locked rows. Callers MUST be inside a transaction: the locks are
    row-scoped and die with it.
    """
    queryset = Obligation.objects.filter(pk__in=subject_ids)
    if client_id is not None:
        queryset = queryset.filter(client_id=client_id)
    return list(queryset.select_for_update(no_key=True, of=("self",)).order_by("pk"))


def _scoped_subject(actor: User, subject_id: UUID) -> Obligation | None:
    """Resolve the obligation through the actor's portfolio, or 404."""
    return Obligation.objects.filter(
        pk=subject_id,
        client_id__in=visible_clients(actor, tenant_id=tenant_id()).values("pk"),
    ).first()


def _staff_scope(actor: User, client_id: UUID) -> User:
    """Hold fresh authority and the portfolio fence before any aggregate lock."""
    require_enabled()
    authority = lock_authorities({actor.pk}, tenant_id=tenant_id())[actor.pk]
    if (
        not authority.user.is_active
        or authority.user.is_superuser
        or authority.membership is None
        or not authority.membership.is_active
    ):
        raise PermissionDenied
    client = (
        ClientCompany.objects.select_for_update(no_key=True)
        .filter(pk=client_id)
        .first()
    )
    if client is None:
        raise Http404
    list(
        ClientAssignment.objects.select_for_update(no_key=True)
        .filter(client=client, user=authority.user)
        .order_by("pk")
    )
    load_client(authority.user, tenant_id(), client.pk)
    require_actor(authority.user, CYCLES_OPERATE)
    return authority.user


def _check_staff_identity(subject: Obligation, kind: str, period: date) -> None:
    if kind not in {ObservationKind.PAYMENT, ObservationKind.GUIDE_ISSUED}:
        msg = "Informe pagamento ou emissão de guia."
        raise ValidationError(msg)
    if subject.competence_month != period:
        raise Http404


def _staff_evidence(
    subject: Obligation,
    evidence_id: UUID | None,
    *,
    cycle: ServiceCycle | None = None,
) -> UUID | None:
    """Bind optional evidence before the subject lock, with no cross-cycle drift."""
    if evidence_id is None:
        return None
    candidate = (
        EvidenceLink.objects.filter(pk=evidence_id, client_id=subject.client_id)
        .select_related("request")
        .first()
    )
    if candidate is None or (
        cycle is not None and candidate.request.cycle_id != cycle.pk
    ):
        raise Http404
    if cycle is None:
        cycle = ServiceCycle.objects.select_for_update(no_key=True).get(
            pk=candidate.request.cycle_id, client_id=subject.client_id
        )
    request = EvidenceRequest.objects.select_for_update(no_key=True).get(
        pk=candidate.request_id, client_id=subject.client_id, cycle=cycle
    )
    link = (
        EvidenceLink.objects.select_for_update(no_key=True, of=("self",))
        .filter(pk=evidence_id, client_id=subject.client_id, request=request)
        .first()
    )
    if (
        link is None
        or cycle.period_start != subject.competence_month
        or request.obligation_id not in {None, subject.pk}
    ):
        raise Http404
    return link.pk


def chain_heads(
    subject_id: UUID, kind: str, period: date | None
) -> list[SourceObservation]:
    """Return the current (non-superseded) assertions of one claim chain.

    A claim chain is one (subject, kind, period): independent receipts are
    sibling roots, and a correction forks only off the head it names. Two
    queries — the rows, then which of them have successors — rather than a
    NOT EXISTS subquery, so the read shape stays identical to the effect
    manifest's post-lock re-check.
    """
    queryset = SourceObservation.objects.filter(subject_ref_id=subject_id, kind=kind)
    queryset = (
        queryset.filter(Q(period=period) | Q(period__isnull=True))
        if period is not None
        else queryset.filter(period__isnull=True)
    )
    rows = list(queryset)
    if not rows:
        return []
    by_id = {row.pk: row for row in rows}
    superseded = {
        row.supersedes_id
        for row in rows
        if row.supersedes_id in by_id and by_id[row.supersedes_id].period == row.period
    }
    return [row for row in rows if row.pk not in superseded]


def assert_fact(  # noqa: PLR0913 - the write contract names every argument
    *,
    tenant_id: UUID,
    client_id: UUID,
    subject_id: UUID,
    kind: str,
    period: date | None,
    source_kind: str,
    observed_at: datetime,
    value: Mapping[str, object],
    verification_status: str,
    outcome: str,
    source_ref: str = "",
    recorded_by: User | None = None,
    supersedes: SourceObservation | None = None,
    evidence_link_id: UUID | None = None,
) -> SourceObservation:
    """Insert one assertion; the caller holds the subject lock and transaction.

    `value` carries identifiers and codes only (the column's contract), so the
    submitted file's digest arrives as `sha256` here and lands in `value_json`
    from exactly one place.
    """
    payload: dict[str, object] = dict(value)
    sha256 = payload.get("sha256", "")
    if sha256 and (not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256)):
        msg = "sha256 must be a lowercase hex digest."
        raise ValidationError(msg)
    row = SourceObservation(
        tenant_id=tenant_id,
        client_id=client_id,
        subject_ref_id=subject_id,
        kind=kind,
        period=period,
        source_kind=source_kind,
        observed_at=observed_at,
        value_json=payload,
        verification_status=verification_status,
        outcome=outcome,
        source_ref=source_ref,
        recorded_by=recorded_by,
        supersedes=supersedes,
        evidence_link_id=evidence_link_id,
    )
    # Related objects are loaded already and the paired FKs are database
    # constraints, so clean() runs without re-fetching them.
    row.full_clean(exclude=("subject_ref", "client", "supersedes", "evidence_link"))
    row.save()
    return row


def record_staff_fact(  # noqa: PLR0913 - the write contract names every argument
    actor: User,
    *,
    subject_id: UUID,
    kind: str,
    period: date,
    verification_status: str = ObservationVerificationStatus.UNKNOWN,
    value: Mapping[str, object] | None = None,
    source_ref: str = "",
    expected_head_id: UUID | None = None,
    evidence_link_id: UUID | None = None,
) -> SourceObservation:
    """Record a staff fact, or supersede the named head with a correction.

    Without `expected_head_id` this is a fresh claim root. With it, the named
    row must still be a chain head — anything else is a stale-snapshot conflict,
    never a silent fork.
    """
    with transaction.atomic():
        subject = _scoped_subject(actor, subject_id)
        if subject is None:
            raise Http404
        fresh_actor = _staff_scope(actor, subject.client_id)
        evidence_id = _staff_evidence(subject, evidence_link_id)
        locked = lock_subjects([subject.pk], client_id=subject.client_id)
        if not locked:
            raise Http404
        subject = locked[0]
        _check_staff_identity(subject, kind, period)
        require_actor(fresh_actor, CYCLES_OPERATE)
        supersedes = _expected_head(subject.pk, kind, period, expected_head_id)
        return assert_fact(
            tenant_id=subject.tenant_id,
            client_id=subject.client_id,
            subject_id=subject.pk,
            kind=kind,
            period=supersedes.period if supersedes is not None else period,
            source_kind=SOURCE_STAFF_RECORDED,
            observed_at=timezone.now(),
            value=value or {},
            verification_status=verification_status,
            outcome=OUTCOME_CORRECTED if supersedes is not None else OUTCOME_RECORDED,
            source_ref=source_ref,
            recorded_by=fresh_actor,
            supersedes=supersedes,
            evidence_link_id=evidence_id,
        )


# retract keeps the same lock discipline as record: portfolio scope, capability
# re-check, subject lock, head re-read — only the written row differs.
def retract_staff_fact(
    actor: User,
    *,
    subject_id: UUID,
    kind: str,
    period: date,
    expected_head_id: UUID,
) -> SourceObservation:
    """Retract a staff fact: a superseding assertion that proves nothing.

    Deleting is impossible (the table is append-only for every role), so the
    honest move is a new head marked `unsupported` — the claim stays readable
    in history and stops counting as knowledge.
    """
    with transaction.atomic():
        subject = _scoped_subject(actor, subject_id)
        if subject is None:
            raise Http404
        fresh_actor = _staff_scope(actor, subject.client_id)
        locked = lock_subjects([subject.pk], client_id=subject.client_id)
        if not locked:
            raise Http404
        subject = locked[0]
        _check_staff_identity(subject, kind, period)
        require_actor(fresh_actor, CYCLES_OPERATE)
        supersedes = _expected_head(subject.pk, kind, period, expected_head_id)
        return assert_fact(
            tenant_id=subject.tenant_id,
            client_id=subject.client_id,
            subject_id=subject.pk,
            kind=kind,
            period=supersedes.period if supersedes is not None else period,
            source_kind=SOURCE_STAFF_RECORDED,
            observed_at=timezone.now(),
            value={},
            verification_status="unsupported",
            outcome=OUTCOME_RETRACTED,
            recorded_by=fresh_actor,
            supersedes=supersedes,
        )


@dataclass(frozen=True, slots=True)
class StaffFact:
    """The intent and observation identity carried by the rendered cycle form."""

    subject_id: UUID
    kind: str
    operation: str
    expected_head_id: UUID | None = None
    source_ref: str = ""
    evidence_link_id: UUID | None = None


def record_cycle_fact(
    actor: User, cycle_id: UUID, fact: StaffFact, *, expected_version: int
) -> SourceObservation:
    """Commit a scoped fact, cycle revision and event as one rendered command."""
    with transaction.atomic():
        candidate = load_cycle(actor, cycle_id)
        fresh_actor = _staff_scope(actor, candidate.client_id)
        cycle = ServiceCycle.objects.select_for_update(no_key=True).get(
            pk=candidate.pk, client_id=candidate.client_id
        )
        check_version(cycle.version, expected_version)
        require_actor(fresh_actor, CYCLES_OPERATE)
        if cycle.state not in OPEN_STATES:
            msg = "Este ciclo está encerrado."
            raise IllegalTransition(msg)
        engagement = Engagement.objects.select_for_update(no_key=True).get(
            pk=cycle.engagement_id, client_id=cycle.client_id
        )
        subject = Obligation.objects.filter(
            pk=fact.subject_id,
            client_id=cycle.client_id,
            competence_month=cycle.period_start,
            obligation_type_id__in=SERVICE_OBLIGATIONS.get(engagement.service_kind, ()),
        ).first()
        profile = profile_at(cycle.client, cycle.period_start)
        if (
            subject is None
            or engagement.starts_on > cycle.period_start
            or (
                engagement.ends_on is not None
                and engagement.ends_on <= cycle.period_start
            )
            or profile is None
            or profile.regime != TaxRegime.MEI
        ):
            raise Http404
        if fact.operation in {"correct", "retract"} and fact.expected_head_id is None:
            msg = "Informe o registro atual para corrigir ou retirar."
            raise ValidationError(msg)
        evidence_id = _staff_evidence(subject, fact.evidence_link_id, cycle=cycle)
        match fact.operation:
            case "retract":
                if fact.expected_head_id is None:
                    msg = "Informe o registro a retirar."
                    raise ValidationError(msg)
                observation = retract_staff_fact(
                    fresh_actor,
                    subject_id=subject.pk,
                    kind=fact.kind,
                    period=cycle.period_start,
                    expected_head_id=fact.expected_head_id,
                )
            case "record" | "correct":
                observation = record_staff_fact(
                    fresh_actor,
                    subject_id=subject.pk,
                    kind=fact.kind,
                    period=cycle.period_start,
                    source_ref=fact.source_ref,
                    expected_head_id=(
                        fact.expected_head_id if fact.operation == "correct" else None
                    ),
                    evidence_link_id=evidence_id,
                )
            case _:
                msg = "Ação de registro desconhecida."
                raise ValidationError(msg)
        cycle.version += 1
        cycle.save(update_fields=["version"])
        emit(
            cycle,
            fresh_actor,
            "observation.recorded",
            payload={"observation_id": str(observation.pk), "kind": fact.kind},
        )
        return observation


def _expected_head(
    subject_id: UUID,
    kind: str,
    period: date | None,
    expected_head_id: UUID | None,
) -> SourceObservation | None:
    """Resolve the predecessor a correction names, under the subject lock."""
    if expected_head_id is None:
        return None
    head = next(
        (
            row
            for row in chain_heads(subject_id, kind, period)
            if row.pk == expected_head_id
        ),
        None,
    )
    if head is None:
        msg = "O registro observado mudou. Recarregue antes de corrigir."
        raise StaleObservationError(msg)
    return head


def calendar_value(subject: Obligation) -> dict[str, str]:
    """Return the deterministic content of a calendar assertion.

    Codes and dates only. The type code, the competence and the two stored
    deadlines are the whole fact — the assertion exists so a later reader can
    distinguish "the schedule was never asserted" from "nobody recorded a
    payment", and a correction to the stored dates produces a superseding row
    instead of a silent drift.
    """
    return {
        "rule_id": subject.obligation_type_id,
        "competence": f"{subject.competence_month:%Y-%m}",
        "nominal_due_date": subject.nominal_due_date.isoformat(),
        "resolved_due_date": subject.resolved_due_date.isoformat(),
    }


@transaction.atomic
def emit_calendar_assertions(subjects: list[Obligation]) -> int:
    """Assert the stored schedule of each subject; replay-safe and locked.

    `subjects` must be the rows as persisted — fresh ORM objects that lost an
    `ignore_conflicts` race carry ids the table never stored, and asserting on
    one would fabricate a subject. Idempotent: a head whose `value_json`
    already matches is skipped, and a corrected schedule supersedes it.
    """
    rows = list(subjects)
    if not rows:
        return 0
    rows = lock_subjects([subject.pk for subject in rows])
    pending = list(
        SourceObservation.objects.filter(
            subject_ref_id__in=[subject.pk for subject in rows],
            kind="calendar_scheduled",
            period__in=[subject.competence_month for subject in rows],
        )
    )
    superseded = (
        set(
            SourceObservation.objects.filter(
                supersedes_id__in=[row.pk for row in pending]
            ).values_list("supersedes_id", flat=True)
        )
        if pending
        else set()
    )
    heads_by_subject: dict[UUID, list[SourceObservation]] = {}
    for row in pending:
        if row.pk not in superseded:
            heads_by_subject.setdefault(row.subject_ref_id, []).append(row)
    written = 0
    for subject in rows:
        value = calendar_value(subject)
        heads = heads_by_subject.get(subject.pk, [])
        if len(heads) > 1:
            msg = (
                f"calendar_scheduled claim for {subject.pk} has "
                f"{len(heads)} heads; refusing to fork it further"
            )
            raise ValidationError(msg)
        if heads and heads[0].value_json == value:
            continue
        assert_fact(
            tenant_id=subject.tenant_id,
            client_id=subject.client_id,
            subject_id=subject.pk,
            kind="calendar_scheduled",
            period=subject.competence_month,
            source_kind=SOURCE_DETERMINISTIC_RULE,
            observed_at=subject.created_at,
            value=value,
            verification_status=ObservationVerificationStatus.UNKNOWN,
            outcome=OUTCOME_SCHEDULED,
            supersedes=heads[0] if heads else None,
        )
        written += 1
    return written
