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

Lock discipline: the subject row is locked `NO KEY UPDATE` (mirroring
`effects/build.py`) so the observation's `FOR KEY SHARE` fence trigger never
transfers a lock onto it. Every writer takes that lock before reading the
chain, so two writers cannot both read "no head" and insert competing roots.
"""

import re
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from typing import Final
from uuid import UUID
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import QuerySet
from django.http import Http404
from django.utils import timezone

from apps.accounts.models import User
from apps.authz.portfolio import visible_clients
from apps.cycles.capabilities import CYCLES_OPERATE
from apps.cycles.services.cycle import require_actor, tenant_id
from apps.obligations.models import (
    Obligation,
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

# The calendar assertion is stamped at a fixed local time two days before the
# resolved deadline: deterministic in content and timestamp, and early enough
# to satisfy "scheduled no later than two days before".
_CALENDAR_OBSERVED_HOUR: Final = 9
_CALENDAR_OBSERVED_LEAD_DAYS: Final = 2
_LOCAL_ZONE: Final = ZoneInfo("America/Sao_Paulo")


class StaleObservationError(ValidationError):
    """The expected chain head moved since the caller's snapshot."""


def lock_subjects(subject_ids: list[UUID]) -> QuerySet[Obligation]:
    """Take the observation subject locks in id order, NO KEY UPDATE.

    Returns the locked rows. Callers MUST be inside a transaction: the locks are
    row-scoped and die with it.
    """
    return (
        Obligation.objects.filter(pk__in=list(subject_ids))
        .select_for_update(no_key=True)
        .order_by("pk")
    )


def _scoped_subject(actor: User, subject_id: UUID) -> Obligation | None:
    """Resolve the obligation through the actor's portfolio, or 404."""
    return Obligation.objects.filter(
        pk=subject_id,
        client_id__in=visible_clients(actor, tenant_id=tenant_id()).values("pk"),
    ).first()


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
        queryset.filter(period=period)
        if period is not None
        else queryset.filter(period__isnull=True)
    )
    rows = list(queryset)
    if not rows:
        return []
    superseded = set(
        SourceObservation.objects.filter(
            supersedes_id__in=[row.pk for row in rows]
        ).values_list("supersedes_id", flat=True)
    )
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
        require_actor(actor, CYCLES_OPERATE)
        lock_subjects([subject.pk])
        supersedes = _expected_head(subject.pk, kind, period, expected_head_id)
        return assert_fact(
            tenant_id=subject.tenant_id,
            client_id=subject.client_id,
            subject_id=subject.pk,
            kind=kind,
            period=period,
            source_kind=SOURCE_STAFF_RECORDED,
            observed_at=timezone.now(),
            value=value or {},
            verification_status=verification_status,
            outcome=OUTCOME_CORRECTED if supersedes is not None else OUTCOME_RECORDED,
            source_ref=source_ref,
            recorded_by=actor,
            supersedes=supersedes,
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
        require_actor(actor, CYCLES_OPERATE)
        lock_subjects([subject.pk])
        supersedes = _expected_head(subject.pk, kind, period, expected_head_id)
        return assert_fact(
            tenant_id=subject.tenant_id,
            client_id=subject.client_id,
            subject_id=subject.pk,
            kind=kind,
            period=period,
            source_kind=SOURCE_STAFF_RECORDED,
            observed_at=timezone.now(),
            value={},
            verification_status="unsupported",
            outcome=OUTCOME_RETRACTED,
            recorded_by=actor,
            supersedes=supersedes,
        )


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


def _calendar_observed_at(subject: Obligation) -> datetime:
    deadline = min(subject.nominal_due_date, subject.resolved_due_date)
    return datetime.combine(
        deadline - timedelta(days=_CALENDAR_OBSERVED_LEAD_DAYS),
        time(_CALENDAR_OBSERVED_HOUR),
        tzinfo=_LOCAL_ZONE,
    )


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
    lock_subjects([subject.pk for subject in rows])
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
            observed_at=_calendar_observed_at(subject),
            value=value,
            verification_status="verified",
            outcome=OUTCOME_SCHEDULED,
            supersedes=heads[0] if heads else None,
        )
        written += 1
    return written
