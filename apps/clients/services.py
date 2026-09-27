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

from datetime import date

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditAction
from apps.audit.services import ObjectRef, record_event
from apps.authz.services import can
from apps.clients.models import (
    AssignmentRole,
    ClientAssignment,
    ClientCompany,
    OnboardingItem,
    OnboardingItemTemplate,
    ProfileConfidence,
    TaxProfile,
)

OBJECT_TYPE = "clients.ClientAssignment"
TAX_PROFILE_OBJECT_TYPE = "clients.TaxProfile"

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


def profile_at(client: ClientCompany, on: date) -> TaxProfile | None:
    """Answer what the system currently believes the client's regime was `on`.

    Returns `None` — not a guess — when no current row covers the date. A gap in the
    timeline is the honest answer the exclusion constraint exists to make
    representable: the day before a client's earliest claim is *unknown*, and
    surfacing it is what keeps a correction from quietly inventing history.
    """
    return (
        TaxProfile.objects.filter(
            client_id=client.id,
            superseded_at__isnull=True,
            valid_from__lte=on,
        )
        .filter(_open_or_ends_after(on))
        .order_by("-valid_from")
        .first()
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
) -> TaxProfile:
    """Assert a new claim about the client's regime, correcting what overlaps it.

    The correction is one transaction: the client's row is locked so two corrections
    cannot interleave, every current row overlapping the new range is stamped
    `superseded_at` (its claim is preserved, never rewritten), slices of those rows
    outside the new range are re-asserted as fresh rows pointing back at the ones
    they continue, and the new claim is inserted last. `correction_of` names the
    row whose claim the correction overruled — the overlapping row with the earliest
    start — so the trail from assertion to assertion stays one hop long.

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
        # Lock the client row so a second correction on the same timeline waits for
        # this one to commit or roll back before reading it. The exclusion
        # constraint polices the range dimension; this lock polices the write side.
        locked_client = ClientCompany.objects.select_for_update().get(pk=client.pk)
        # The capability re-check sits INSIDE the lock: the row it protects and the
        # permission it requires are settled in the same instant, so a revoked
        # assignment cannot outrun the check.
        if not can(actor, "clients.edit_tax_profile", locked_client):
            msg = (
                f"{actor} lacks clients.edit_tax_profile on client {client.pk}: "
                "a tax-timeline correction needs the capability, not only a view."
            )
            raise PermissionDenied(msg)

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
    "assign_client",
    "correct_tax_profile",
    "create_default_checklist",
    "profile_at",
    "unassign_client",
]
