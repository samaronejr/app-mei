"""The sanctioned write paths for portfolio changes.

Assignment is what the `limited` permission level resolves against, so adding or
removing one silently changes who can read a client's tax and revenue data. Every
change therefore leaves an `audit.Event` naming the actor, the client and the account
whose reach moved.

`correct_tax_profile` extends the same discipline to the client's tax timeline: a
correction is a transaction that locks the timeline, stamps `superseded_at` on the
current rows it overlaps, restates their uncovered slices, and writes one event —
never an UPDATE that rewrites what the firm believed.
"""

from collections.abc import Iterable
from datetime import date
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditAction
from apps.audit.services import ObjectRef, record_event
from apps.authz.portfolio import visible_clients
from apps.authz.services import can
from apps.clients.models import (
    AssignmentRole,
    ClientAssignment,
    ClientCompany,
    OnboardingItem,
    OnboardingItemTemplate,
    ProfileConfidence,
    TaxProfile,
    TaxRegime,
)

OBJECT_TYPE = "clients.ClientAssignment"
TAX_PROFILE_OBJECT_TYPE = "clients.TaxProfile"


class TaxProfileConflictError(Exception):
    """Raised when a correction's expected revision no longer matches the timeline.

    A conflict, not a permission failure — the view layer maps it to 409 the same
    way `MemberLifecycleError` is rendered, because the caller held the capability
    and the row; what they no longer hold is a current snapshot.
    """


# A date far enough out to stand in for "open-ended" when comparing two ranges in
# Python. Only used for the split arithmetic — the stored value stays NULL, because
# a real far-future date would record an expiry nobody set.
FAR_FUTURE = date.max


def assign_client(
    *,
    actor: User,
    client: ClientCompany,
    user: User,
    role: str = AssignmentRole.PRIMARY,
) -> ClientAssignment:
    """Put an account on a client, recording who did it."""
    assignment = ClientAssignment(
        tenant_id=client.tenant_id,
        client=client,
        user=user,
        role=role,
    )
    assignment.save()
    _record(actor=actor, assignment=assignment, change="assigned")
    return assignment


def unassign_client(*, actor: User, assignment: ClientAssignment) -> None:
    """Take an account off a client, recording who did it.

    The event is written **before** the delete so the row's identifiers are still
    readable, and both land in the same request transaction — a rollback discards
    the pair together rather than leaving a record of a removal that did not happen.
    """
    _record(actor=actor, assignment=assignment, change="unassigned")
    assignment.delete()


def _record(*, actor: User, assignment: ClientAssignment, change: str) -> None:
    record_event(
        action=AuditAction.ASSIGNMENT_CHANGED,
        tenant_id=assignment.tenant_id,
        actor=actor,
        obj=ObjectRef(type=OBJECT_TYPE, id=str(assignment.pk)),
        # References only. The audit trail is append-only and LGPD grants erasure,
        # so a client's name or CNPJ written here would be unerasable personal data.
        metadata={
            "change": change,
            "client_id": str(assignment.client_id),
            "user_id": str(assignment.user_id),
            "role": assignment.role,
        },
    )


def create_default_checklist(client: ClientCompany) -> list[OnboardingItem]:
    """Give a newly registered client the standard checklist.

    Driven entirely by the template table, so a Phase 3 connector adds its own item
    by inserting a row rather than by shipping a migration and a code change.

    The key and label are copied onto each item rather than referenced: a template
    row may later be retired or reworded, and a client's checklist must keep saying
    what was actually asked of them at the time.
    """
    templates = OnboardingItemTemplate.objects.filter(is_active=True)
    return OnboardingItem.objects.bulk_create(
        [
            OnboardingItem(
                tenant_id=client.tenant_id,
                client=client,
                key=template.key,
                label=template.label,
                position=template.position,
                requires_ecac=template.requires_ecac,
            )
            for template in templates
        ],
    )


def _open_or_ends_after(on: date) -> Q:
    """Bound the half-open range's top: `valid_to` NULL means open-ended."""
    return Q(valid_to__isnull=True) | Q(valid_to__gt=on)


def claim_at(
    rows: Iterable[TaxProfile],
    on: date,
) -> TaxProfile | None:
    """Pick the current claim covering `on` from rows already in memory.

    The in-memory half of `profile_at`: the exclusion constraint guarantees at most
    one *current* row covers any day, so "latest start among the covering rows" is
    a selection rule, never arbitration. Superseded rows are skipped rather than
    trusted to be absent, because callers fetch with a broad filter and history is
    precisely what `superseded_at` leaves behind.

    The half-open rule mirrors `_open_or_ends_after`: `valid_to` NULL means
    open-ended, and the boundary day belongs to the successor, never to both.
    """
    covering = [
        row
        for row in rows
        if row.superseded_at is None
        and row.valid_from <= on
        and (row.valid_to is None or row.valid_to > on)
    ]
    if not covering:
        return None
    return max(covering, key=lambda row: row.valid_from)


def profile_at(client: ClientCompany, on: date) -> TaxProfile | None:
    """Answer what the system currently believes the client's regime was `on`.

    Returns `None` — not a guess — when no current row covers the date. A gap in the
    timeline is the honest answer the exclusion constraint exists to make
    representable: the day before a client's earliest claim is *unknown*, and
    surfacing it is what keeps a correction from quietly inventing history.

    Resolved through `claim_at` so the half-open rule is written once and the
    per-client path can never drift from the batched one below.
    """
    return claim_at(
        TaxProfile.objects.filter(client_id=client.id),
        on,
    )


def current_profiles(clients: Iterable[ClientCompany]) -> dict[UUID, list[TaxProfile]]:
    """Fetch every tax profile of the given clients in one query, keyed by client.

    The batched half of the timeline: a portfolio sweep or queue that called
    `profile_at` per client would read this table once per client, which is the N+1
    the queue budgets exist to forbid. Rows are grouped and sorted newest-first so
    `claim_at` answers the same way `profile_at` does. A client with no rows maps
    to no key — the caller reads `.get(client.id, [])`, and the empty list is the
    honest "nobody has ever claimed a regime for this company".
    """
    ids = [client.pk for client in clients]
    if not ids:
        return {}
    grouped: dict[UUID, list[TaxProfile]] = {}
    for row in (
        TaxProfile.objects.filter(client_id__in=ids)
        .order_by("client_id", "-valid_from")
        .iterator()
    ):
        grouped.setdefault(row.client_id, []).append(row)
    return grouped


def lock_tax_profile_client(
    *, actor: User, client: ClientCompany, expected_revision: int
) -> ClientCompany:
    """Authorize a snapshot under the row lock; callers hold the transaction open."""
    locked = (
        visible_clients(actor, tenant_id=client.tenant_id)
        .select_for_update()
        .filter(pk=client.pk)
        .first()
    )
    if locked is None:
        raise Http404
    if not can(actor, "clients.edit_tax_profile", locked):
        msg = f"{actor} lacks clients.edit_tax_profile on client {client.pk}"
        raise PermissionDenied(msg)
    if locked.tax_profile_revision != expected_revision:
        msg = (
            f"client {client.pk} tax timeline is at revision "
            f"{locked.tax_profile_revision}, not {expected_revision}"
        )
        raise TaxProfileConflictError(msg)
    return locked


@transaction.atomic
def save_client_registry(*, actor: User, client: ClientCompany, created: bool) -> None:
    """Save legacy fields and their claim only after checking the locked revision."""
    if created:
        if not can(actor, "clients.edit_tax_profile", client):
            raise PermissionDenied
    else:
        lock_tax_profile_client(
            actor=actor, client=client, expected_revision=client.tax_profile_revision
        )
    client.save()
    sync_profile_flag(actor=actor, client=client, created=created)
    client.refresh_from_db(fields=["tax_profile_revision"])


@transaction.atomic
def sync_profile_flag(
    *,
    actor: User,
    client: ClientCompany,
    created: bool,
    today: date | None = None,
) -> TaxProfile | None:
    """Write the profile row the legacy `is_mei`/`mei_category` fields now claim.

    The dual-write half of the flag's retirement: callers keep writing `is_mei` and
    `mei_category` exactly as before, and this service carries the same claim into
    the effective-dated timeline. On creation the claim opens at the earlier of the
    client's opening date and today — matching what `backfill_v1` asserted for the
    rows it migrated — and on edit it opens today, because a flag change asserts
    the regime the client is under NOW; history before it stays with the claims
    that were already believed.

    The edit path goes through `correct_tax_profile`, so superseding, audit and the
    capability check are the same machinery a deliberate correction uses. A save
    whose derived claim equals today's adds no row: dual-write is an invariant over
    content, not a write-per-save ritual.
    """
    lock_tax_profile_client(
        actor=actor, client=client, expected_revision=client.tax_profile_revision
    )
    today = today or timezone.localdate()
    regime = TaxRegime.MEI if client.is_mei else TaxRegime.UNKNOWN
    category = client.mei_category if client.is_mei else None

    if not created:
        current = profile_at(client, today)
        if (
            current is not None
            and current.regime == regime
            and current.mei_category == category
        ):
            return None
        valid_from = today
    else:
        valid_from = min(client.opened_on or today, today)

    return correct_tax_profile(
        client=client,
        regime=regime,
        valid_from=valid_from,
        actor=actor,
        recorded_by=actor,
        provenance="dual_write",
        confidence=ProfileConfidence.STAFF_RECORDED,
        note=(
            "Asserted by the client registry's dual-write of the legacy "
            "is_mei/mei_category fields."
        ),
        mei_category=category,
        expected_revision=client.tax_profile_revision,
    )


def correct_tax_profile(  # noqa: PLR0913
    # The signature is the correction contract: every parameter exists in the
    # bitemporal row it writes, and keyword-only is what keeps callers honest.
    *,
    client: ClientCompany,
    regime: str,
    valid_from: date,
    valid_to: date | None = None,
    actor: User,
    recorded_by: User | None = None,
    provenance: str = "staff_entry",
    confidence: str = ProfileConfidence.STAFF_RECORDED,
    note: str = "",
    mei_category: str | None = None,
    expected_revision: int,
) -> TaxProfile:
    """Assert a new claim about the client's regime, correcting what overlaps it.

    The correction is one transaction: the client is resolved through the actor's
    portfolio — an invisible client answers `Http404`, refusing to confirm the id
    names a real row — and its row is locked so two corrections cannot interleave.
    Every current row overlapping the new range is stamped `superseded_at` (its
    claim is preserved, never rewritten), slices of those rows outside the new
    range are re-asserted as fresh rows pointing back at the ones they continue,
    and the new claim is inserted last. `correction_of` names the row whose claim
    the correction overruled — the overlapping row with the earliest start — so
    the trail from assertion to assertion stays one hop long.

    `expected_revision` is the optimistic-concurrency contract: the caller passes
    the `tax_profile_revision` it read, and the write proceeds only while that
    number is still current. Two callers holding the same snapshot cannot both
    commit — the second raises `TaxProfileConflictError` before any row moves.

    `actor` is who performed the correction (audited and re-checked against
    `clients.edit_tax_profile`, so a caller that lost the capability mid-request
    is refused here rather than trusted from a stale page); `recorded_by` is who
    the claim is attributed to on the row, which differs when staff records a
    claim on someone else's instruction.
    """
    if valid_to is not None and valid_to <= valid_from:
        msg = (
            "valid_to must be after valid_from; "
            "a closed or empty range asserts nothing."
        )
        raise ValueError(msg)

    with transaction.atomic():
        locked_client = lock_tax_profile_client(
            actor=actor, client=client, expected_revision=expected_revision
        )

        now = timezone.now()
        # Half-open overlap: a row's range [valid_from, valid_to) meets the
        # correction's [valid_from, valid_to) when it starts before the new end and
        # ends after the new start. An open-ended correction bounds no start, so the
        # `valid_from` arm is dropped rather than compared against a fake infinity.
        overlap_q = Q(client_id=client.id, superseded_at__isnull=True) & (
            _open_or_ends_after(valid_from)
        )
        if valid_to is not None:
            overlap_q &= Q(valid_from__lt=valid_to)
        overlapping = list(
            TaxProfile.objects.select_for_update()
            .filter(overlap_q)
            .order_by("valid_from")
        )

        replaced = overlapping[0] if overlapping else None
        restated: list[TaxProfile] = []
        for row in overlapping:
            row.superseded_at = now
            row.save(update_fields=["superseded_at"])
            # Whatever the row claimed outside the corrected range is re-asserted as
            # its own row — superseding removes the belief, not the record, and the
            # uncovered slices are still believed.
            if row.valid_from < valid_from:
                restated.append(
                    TaxProfile(
                        tenant_id=row.tenant_id,
                        client_id=row.client_id,
                        regime=row.regime,
                        mei_category=row.mei_category,
                        valid_from=row.valid_from,
                        valid_to=valid_from,
                        provenance=row.provenance,
                        confidence=row.confidence,
                        recorded_by_id=row.recorded_by_id,
                        recorded_at=now,
                        correction_of_id=row.id,
                        note=row.note,
                    )
                )
            # The correction's own upper bound, or the far future when it is
            # open-ended: either way a slice survives only past that point. The
            # None check for valid_to doubles as mypy's narrowing — inside this
            # branch the tail fragment's start is always a real date.
            new_end = valid_to or FAR_FUTURE
            row_end = row.valid_to or FAR_FUTURE
            if row_end > new_end and valid_to is not None:
                restated.append(
                    TaxProfile(
                        tenant_id=row.tenant_id,
                        client_id=row.client_id,
                        regime=row.regime,
                        mei_category=row.mei_category,
                        valid_from=valid_to,
                        valid_to=row.valid_to,
                        provenance=row.provenance,
                        confidence=row.confidence,
                        recorded_by_id=row.recorded_by_id,
                        recorded_at=now,
                        correction_of_id=row.id,
                        note=row.note,
                    )
                )
        if restated:
            TaxProfile.objects.bulk_create(restated)

        created = TaxProfile.objects.create(
            tenant_id=client.tenant_id,
            client_id=client.id,
            regime=regime,
            mei_category=mei_category,
            valid_from=valid_from,
            valid_to=valid_to,
            provenance=provenance,
            confidence=confidence,
            recorded_by=recorded_by or actor,
            recorded_at=now,
            correction_of=replaced,
            note=note,
        )
        # Advance the revision with the rest of the correction so a loser can
        # never observe an unchanged counter beside a changed timeline. The client
        # row is locked, so read-modify-write in Python is safe here.
        locked_client.tax_profile_revision += 1
        locked_client.save(update_fields=["tax_profile_revision"])

        record_event(
            action=AuditAction.TAX_PROFILE_CORRECTED,
            tenant_id=client.tenant_id,
            actor=actor,
            obj=ObjectRef(type=TAX_PROFILE_OBJECT_TYPE, id=str(created.pk)),
            metadata={
                "client_id": str(client.id),
                "regime": regime,
                "valid_from": valid_from.isoformat(),
                "valid_to": valid_to.isoformat() if valid_to else None,
                "superseded": [str(row.id) for row in overlapping],
                "restated": [str(row.id) for row in restated],
            },
        )
        return created


__all__ = [
    "TaxProfileConflictError",
    "assign_client",
    "claim_at",
    "correct_tax_profile",
    "create_default_checklist",
    "current_profiles",
    "lock_tax_profile_client",
    "profile_at",
    "save_client_registry",
    "sync_profile_flag",
    "unassign_client",
]
