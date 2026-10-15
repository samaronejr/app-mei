"""In-product portfolio maintenance: the write paths behind the onboarding screens.

Every function here is a firm-side state change and follows the same four-step
discipline (plan "Must have"): the client is loaded through the actor's portfolio
(`load_client`, 404 otherwise) and LOCKED, the capability is re-checked inside the
lock, the version the caller read is compared and a stale one raises a typed conflict
before anything is written, and one audit event records the change with references
only.

Capabilities are the S8.1 rows AXR1-004 seeded: `clients.manage` for clients,
engagements and contacts, and `users.create` for assignment changes and portal
invitations, which S8.1 keeps on that existing capability.

The legacy `is_mei`/`mei_category` fields and the tax timeline must agree
(`tests/clients/test_legacy_flag_agreement.py`). A client created here or by an
import opens its timeline with the claim the flag makes (`open_initial_claim`), and
a timeline edit from the onboarding page brings the flag back in line with today's
claim under the same lock (`set_tax_profile`).
"""

import hashlib
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Final
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.http import Http404
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.accounts.invites import issue_contact_invite
from apps.accounts.models import User
from apps.audit.models import AuditAction
from apps.audit.services import ObjectRef, Origin, record_event, record_platform_event
from apps.authz.objects import load_visible_client
from apps.authz.portfolio import PortfolioScope, portfolio_scope, visible_clients
from apps.authz.services import can
from apps.clients.models import (
    AssignmentRole,
    ClientAssignment,
    ClientCompany,
    ClientContact,
    Engagement,
    ProfileConfidence,
    TaxProfile,
    TaxRegime,
)
from apps.clients.services import (
    TaxProfileConflictError,
    assign_client,
    correct_tax_profile,
    profile_at,
    unassign_client,
)
from apps.fiscal.validators import normalize_document
from apps.tenants.entitlements import (
    ACTIVE_CLIENT_STATES,
    lock_entitlement,
    require_client_capacity,
)
from apps.tenants.models import Invite, Membership, Tenant

if TYPE_CHECKING:
    from django.db.models import QuerySet

MANAGE_CAPABILITY: Final = "clients.manage"
# S8.1: "Assignment changes additionally require users.create (existing)".
ASSIGN_CAPABILITY: Final = "users.create"

STAFF_PROVENANCE: Final = "staff_entry"

# The identity fields the editor may change. The CNPJ is not among them: it is the
# firm-wide identity key the duplicate check and every import dedupe against.
EDITABLE_FIELDS: Final[tuple[str, ...]] = (
    "legal_name",
    "trade_name",
    "cpf",
    "state",
    "municipality_ibge_code",
    "main_cnae",
    "opened_on",
    "status",
)


class ConflictError(Exception):
    """The row moved since the caller read it; nothing was written (HTTP 409)."""


class DuplicateClientError(ValidationError):
    """The CNPJ is already on this firm's books (never checked across firms)."""


class RetroNoteRequiredError(ValidationError):
    """A correction of a past period that already had a belief needs a note."""


def load_client(
    actor: User,
    tenant_id: UUID,
    client_id: UUID,
    *,
    lock: bool = False,
) -> ClientCompany:
    """Return the client if the actor's portfolio holds it, else raise Http404.

    Unlocked reads go through AXR1-004's `load_visible_client`. A locking read keeps
    the visibility filter INSIDE the locked query (the `correct_tax_profile`
    pattern), so the row is resolved through the portfolio and locked in one
    statement. 404, never 403: the id must not confirm a client outside the book.
    """
    if not lock:
        return load_visible_client(actor, client_id, tenant_id=tenant_id)
    queryset: QuerySet[ClientCompany] = visible_clients(actor, tenant_id=tenant_id)
    client = queryset.select_for_update().filter(pk=client_id).first()
    if client is None:
        raise Http404
    return client


def open_initial_claim(
    *,
    client: ClientCompany,
    recorded_by: User | None,
    provenance: str,
    today: date | None = None,
) -> TaxProfile:
    """Open a brand-new client's timeline with the claim its legacy flag makes.

    The creation half of the dual-write, for a client that has no timeline yet:
    there is nothing to supersede, so this inserts the one row that makes `is_mei`
    and `profile_at(today)` agree, from the earlier of the opening date and today
    (as `sync_profile_flag` and `backfill_v1` do). Marked
    `asserted_by_legacy_flag` because nobody verified the regime — the flag's
    default did — and the setup page labels it unverified. Not routed through
    `correct_tax_profile`: its `clients.edit_tax_profile` check is LIMITED for an
    operations admin on an unassigned client, which would forbid a role holding
    `clients.manage`/`clients.import` from registering clients at all. Edits of
    the timeline afterwards do go through `correct_tax_profile`.
    """
    today = today or timezone.localdate()
    return TaxProfile.objects.create(
        tenant_id=client.tenant_id,
        client_id=client.pk,
        regime=TaxRegime.MEI if client.is_mei else TaxRegime.UNKNOWN,
        mei_category=client.mei_category if client.is_mei else None,
        valid_from=min(client.opened_on or today, today),
        provenance=provenance,
        confidence=ProfileConfidence.ASSERTED_BY_LEGACY_FLAG,
        recorded_by=recorded_by,
        note="Aberto pelo cadastro a partir do campo MEI; não verificado.",
    )


def _require(actor: User, capability: str, tenant_id: UUID) -> None:
    if not can(actor, capability, tenant_id=tenant_id):
        msg = f"{actor} lacks {capability}."
        raise PermissionDenied(msg)


def _event(
    action: str,
    *,
    actor: User,
    tenant_id: UUID,
    obj: ObjectRef,
    metadata: dict[str, object],
) -> None:
    record_event(
        action=action,
        tenant_id=tenant_id,
        actor=actor,
        obj=obj,
        metadata=metadata,
    )


def _client_ref(client: ClientCompany) -> ObjectRef:
    return ObjectRef(type="clients.ClientCompany", id=str(client.pk))


def find_duplicate(tenant_id: UUID, cnpj: str) -> ClientCompany | None:
    """Return this firm's client holding `cnpj`, if any.

    Tenant-scoped on purpose and ONLY tenant-scoped: another firm holding the same
    CNPJ is not a duplicate (plan Must NOT: no cross-tenant dedupe), and the scoped
    manager plus RLS make that firm's rows unreachable from here anyway.
    """
    return ClientCompany.objects.filter(
        tenant_id=tenant_id,
        cnpj=normalize_document(cnpj),
    ).first()


@dataclass(frozen=True)
class ClientData:
    """The identity fields a person types into the create form."""

    legal_name: str
    cnpj: str
    trade_name: str = ""
    cpf: str | None = None
    state: str = ""
    municipality_ibge_code: str = ""
    main_cnae: str = ""
    opened_on: date | None = None


def create_client(*, actor: User, tenant_id: UUID, data: ClientData) -> ClientCompany:
    """Register one client, refusing a CNPJ already on this firm's books.

    A staff accountant's portfolio is their assignments, so a client they create is
    assigned to them in the same transaction — otherwise the next page would 404 on
    the record they just typed in.
    """
    with transaction.atomic():
        tenant = lock_entitlement(tenant_id)
        _require(actor, MANAGE_CAPABILITY, tenant_id)
        cnpj = normalize_document(data.cnpj)
        if find_duplicate(tenant_id, cnpj) is not None:
            raise DuplicateClientError(
                {"cnpj": _("Este CNPJ já está cadastrado neste escritório.")},
            )
        require_client_capacity(tenant)
        client = ClientCompany(
            tenant_id=tenant_id,
            legal_name=data.legal_name.strip(),
            trade_name=data.trade_name.strip(),
            cnpj=cnpj,
            cpf=data.cpf or None,
            state=data.state,
            municipality_ibge_code=data.municipality_ibge_code,
            main_cnae=data.main_cnae,
            opened_on=data.opened_on,
        )
        client.full_clean(exclude=["tenant", "import_batch"], validate_unique=False)
        try:
            with transaction.atomic():
                client.save()
        except IntegrityError as error:
            # A concurrent create of the same CNPJ won the per-tenant unique index.
            raise DuplicateClientError(
                {"cnpj": _("Este CNPJ já está cadastrado neste escritório.")},
            ) from error
        open_initial_claim(client=client, recorded_by=actor, provenance="dual_write")
        if portfolio_scope(actor, tenant_id=tenant_id) is PortfolioScope.ASSIGNED:
            assign_client(actor=actor, client=client, user=actor)
        _event(
            AuditAction.CLIENT_CREATED,
            actor=actor,
            tenant_id=tenant_id,
            obj=_client_ref(client),
            metadata={"client_id": str(client.pk), "source": "form"},
        )
        return client


def update_client(
    *,
    actor: User,
    tenant_id: UUID,
    client_id: UUID,
    expected_version: int,
    changes: dict[str, object],
) -> ClientCompany:
    """Apply an identity edit if the row is still at `expected_version`."""
    unknown = set(changes) - set(EDITABLE_FIELDS)
    if unknown:
        msg = f"not editable here: {sorted(unknown)}"
        raise ValueError(msg)
    with transaction.atomic():
        tenant = lock_entitlement(tenant_id)
        client = load_client(actor, tenant_id, client_id, lock=True)
        _require(actor, MANAGE_CAPABILITY, tenant_id)
        if client.version != expected_version:
            msg = (
                f"client {client_id} is at version {client.version}, "
                f"not {expected_version}"
            )
            raise ConflictError(msg)
        requested_status = changes.get("status", client.status)
        if (
            requested_status in ACTIVE_CLIENT_STATES
            and client.status not in ACTIVE_CLIENT_STATES
        ):
            require_client_capacity(tenant)
        for field, value in changes.items():
            setattr(client, field, value)
        if client.cpf == "":
            client.cpf = None
        client.full_clean(exclude=["tenant", "import_batch"], validate_unique=False)
        client.version += 1
        client.save(update_fields=[*changes, "version", "updated_at"])
        _event(
            AuditAction.CLIENT_UPDATED,
            actor=actor,
            tenant_id=tenant_id,
            obj=_client_ref(client),
            metadata={"client_id": str(client.pk), "fields": sorted(changes)},
        )
        return client


def _assert_on_roster(tenant_id: UUID, user_id: UUID | None) -> None:
    if user_id is None:
        return
    # PLATFORM_QUERY_OK: the roster check itself, keyed on this tenant.
    on_roster = Membership.objects.filter(
        user_id=user_id,
        tenant_id=tenant_id,
        is_active=True,
        client__isnull=True,
    ).exists()
    if not on_roster:
        raise ValidationError(
            {"responsible": _("Escolha alguém da equipe deste escritório.")},
        )


@dataclass(frozen=True)
class EngagementData:
    """The fields of the engagement editor."""

    service_kind: str
    starts_on: date
    ends_on: date | None = None
    responsible_id: UUID | None = None


def save_engagement(  # noqa: PLR0913 - keyword-only edit contract
    *,
    actor: User,
    tenant_id: UUID,
    client_id: UUID,
    data: EngagementData,
    engagement_id: UUID | None = None,
    expected_version: int | None = None,
) -> Engagement:
    """Create an engagement, or update one still at `expected_version`."""
    with transaction.atomic():
        client = load_client(actor, tenant_id, client_id, lock=True)
        _require(actor, MANAGE_CAPABILITY, tenant_id)
        _assert_on_roster(tenant_id, data.responsible_id)
        if engagement_id is None:
            engagement = Engagement(
                tenant_id=tenant_id,
                client=client,
                provenance=STAFF_PROVENANCE,
            )
        else:
            found = (
                Engagement.objects.select_for_update()
                .filter(client=client, pk=engagement_id)
                .first()
            )
            if found is None:
                raise Http404
            if found.version != expected_version:
                msg = (
                    f"engagement {engagement_id} is at version "
                    f"{found.version}, not {expected_version}"
                )
                raise ConflictError(msg)
            engagement = found
            engagement.version += 1
        engagement.service_kind = data.service_kind
        engagement.starts_on = data.starts_on
        engagement.ends_on = data.ends_on
        engagement.responsible_id = data.responsible_id
        engagement.full_clean(exclude=["tenant", "client"], validate_unique=False)
        try:
            with transaction.atomic():
                engagement.save()
        except IntegrityError as error:
            raise ValidationError(
                {
                    "starts_on": _(
                        "Este serviço já começa nesta data para este cliente.",
                    ),
                },
            ) from error
        _event(
            AuditAction.ENGAGEMENT_CHANGED,
            actor=actor,
            tenant_id=tenant_id,
            obj=ObjectRef(type="clients.Engagement", id=str(engagement.pk)),
            metadata={
                "client_id": str(client.pk),
                "change": "created" if engagement_id is None else "updated",
                "service_kind": engagement.service_kind,
            },
        )
        return engagement


def _current_belief_overlaps(
    client: ClientCompany,
    valid_from: date,
    valid_to: date | None,
    today: date,
) -> bool:
    """Report whether a current claim already covers part of the past being edited."""
    past_end = min(valid_to, today) if valid_to is not None else today
    if valid_from >= past_end:
        return False
    rows = TaxProfile.objects.filter(
        client_id=client.pk,
        superseded_at__isnull=True,
        valid_from__lt=past_end,
    ).exclude(valid_to__lte=valid_from)
    return rows.exists()


def set_tax_profile(  # noqa: PLR0913 - the correction contract, keyword-only
    *,
    actor: User,
    tenant_id: UUID,
    client_id: UUID,
    regime: str,
    valid_from: date,
    expected_revision: int,
    valid_to: date | None = None,
    mei_category: str | None = None,
    note: str = "",
) -> TaxProfile:
    """Record a tax-profile claim through `correct_tax_profile`, flag kept in step.

    After the correction, still inside the transaction that holds the client lock,
    the legacy `is_mei`/`mei_category` fields are set from the claim that covers
    today, so the flag and the timeline never disagree (the reverse direction of
    T6's dual-write, for an edit that starts on the timeline).

    A RETRO edit — one that restates a past period the firm already held a belief
    about — requires a note, because it rewrites what the firm acted on. Filling a
    period nobody had recorded (a new client's history) is not a retro edit. The
    revision guards the snapshot this judgement was made on: if the timeline moved
    in between, `correct_tax_profile` raises the conflict before writing anything.
    """
    client = load_client(actor, tenant_id, client_id)
    today = timezone.localdate()
    if not note.strip() and _current_belief_overlaps(
        client,
        valid_from,
        valid_to,
        today,
    ):
        raise RetroNoteRequiredError(
            {
                "note": _(
                    "Alterar um período passado exige uma observação explicando "
                    "o motivo.",
                ),
            },
        )
    try:
        with transaction.atomic():
            created = correct_tax_profile(
                client=client,
                regime=regime,
                valid_from=valid_from,
                valid_to=valid_to,
                actor=actor,
                mei_category=mei_category,
                note=note.strip(),
                expected_revision=expected_revision,
            )
            _align_legacy_flag(client, today)
            return created
    except TaxProfileConflictError as error:
        raise ConflictError(str(error)) from error


def _align_legacy_flag(client: ClientCompany, today: date) -> None:
    """Set `is_mei`/`mei_category` from today's claim (row already locked)."""
    current = profile_at(client, today)
    is_mei = current is not None and current.regime == TaxRegime.MEI
    updates: dict[str, object] = {"is_mei": is_mei}
    if is_mei and current is not None and current.mei_category:
        updates["mei_category"] = current.mei_category
    ClientCompany.objects.filter(pk=client.pk).update(**updates)


@dataclass(frozen=True)
class ContactData:
    """The fields of the contact editor."""

    name: str
    email: str = ""
    phone: str = ""
    role_label: str = ""
    channel_email_allowed: bool = True
    is_primary: bool = False
    active: bool = True


def save_contact(  # noqa: PLR0913 - keyword-only edit contract
    *,
    actor: User,
    tenant_id: UUID,
    client_id: UUID,
    data: ContactData,
    contact_id: UUID | None = None,
    expected_version: int | None = None,
) -> ClientContact:
    """Create a contact, or update one still at `expected_version`.

    Deactivation is an update (`active=False`), never a delete: a contact who asked
    not to be emailed must stay recorded, or the refusal is erased with them.
    """
    with transaction.atomic():
        client = load_client(actor, tenant_id, client_id, lock=True)
        _require(actor, MANAGE_CAPABILITY, tenant_id)
        if contact_id is None:
            contact = ClientContact(tenant_id=tenant_id, client=client)
        else:
            found = (
                ClientContact.objects.select_for_update()
                .filter(client=client, pk=contact_id)
                .first()
            )
            if found is None:
                raise Http404
            if found.version != expected_version:
                msg = (
                    f"contact {contact_id} is at version {found.version}, "
                    f"not {expected_version}"
                )
                raise ConflictError(msg)
            found.version += 1
            contact = found
        contact.name = data.name.strip()
        contact.email = data.email.strip()
        contact.phone = data.phone.strip()
        contact.role_label = data.role_label.strip()
        contact.channel_email_allowed = data.channel_email_allowed
        contact.is_primary = data.is_primary
        contact.active = data.active
        contact.full_clean(exclude=["tenant", "client", "membership"])
        contact.save()
        _event(
            AuditAction.CONTACT_CHANGED,
            actor=actor,
            tenant_id=tenant_id,
            obj=ObjectRef(type="clients.ClientContact", id=str(contact.pk)),
            metadata={
                "client_id": str(client.pk),
                "change": "created" if contact_id is None else "updated",
                "is_active": contact.active,
            },
        )
        return contact


def load_contact(
    actor: User,
    tenant_id: UUID,
    client_id: UUID,
    contact_id: UUID,
) -> tuple[ClientCompany, ClientContact]:
    """Return a contact of a client in the actor's portfolio, else Http404."""
    client = load_client(actor, tenant_id, client_id)
    contact = ClientContact.objects.filter(client=client, pk=contact_id).first()
    if contact is None:
        raise Http404
    return client, contact


class InvitePendingError(ConflictError):
    """The contact already holds a live invitation for this client (HTTP 409)."""


def invite_contact(  # noqa: PLR0913 - keyword-only issuance contract
    *,
    actor: User,
    tenant: Tenant,
    client_id: UUID,
    contact_id: UUID,
    role: str,
    expected_version: int,
) -> tuple[Invite, str]:
    """Issue ONE portal invitation for a contact, judged on its current row.

    Locks the client (portfolio-scoped, 404 otherwise) and then the contact — the
    order `save_contact` takes them in — so an edit and an invitation cannot
    interleave. Under those locks it re-checks the capability, the contact version
    the page was rendered with, the contact's CURRENT permission to be e-mailed, and
    that no live invitation exists for this address and client. Two racing requests
    therefore serialize on the client row, and the second sees the first's committed
    invitation and gets `InvitePendingError`; a request built from a page read
    before the contact was deactivated gets `ConflictError`. Nothing is written on
    either refusal.

    Returns the invitation and its one-time raw token. Sending it is the CALLER's
    job, after this transaction commits: no external I/O happens in here.
    """
    with transaction.atomic():
        client = load_client(actor, tenant.pk, client_id, lock=True)
        contact = (
            ClientContact.objects.select_for_update()
            .filter(client=client, pk=contact_id)
            .first()
        )
        if contact is None:
            raise Http404
        _require(actor, ASSIGN_CAPABILITY, tenant.pk)
        if contact.version != expected_version:
            msg = (
                f"contact {contact_id} is at version {contact.version}, "
                f"not {expected_version}"
            )
            raise ConflictError(msg)
        if not (contact.active and contact.email and contact.channel_email_allowed):
            raise ValidationError(
                _(
                    "Este contato não tem e-mail autorizado. Cadastre um e-mail e "
                    "marque que ele pode receber e-mails antes de convidar.",
                ),
            )
        # PLATFORM_QUERY_OK: tenants_invite has no row-level policy; scoped here to
        # this firm, this client and this address, read under the client lock.
        pending = Invite.objects.filter(
            tenant_id=tenant.pk,
            client_id=client.pk,
            email__iexact=contact.email,
            accepted_at__isnull=True,
            revoked_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).exists()
        if pending:
            msg = f"contact {contact_id} already holds a live invitation"
            raise InvitePendingError(msg)
        return issue_contact_invite(
            actor=actor,
            tenant=tenant,
            role=role,
            contact=contact,
        )


def withdraw_undelivered_invite(
    *,
    invite_id: UUID,
    requested_by: User,
    reason: str,
) -> None:
    """System compensation: stand down ONE invitation whose delivery failed.

    Called only from the post-commit send of the request that issued `invite_id`,
    with that request's own invitation. It is bookkeeping, not an administrator's
    act: it grants nothing and only withdraws a credential nobody received, so it
    does NOT re-check the requester's current `users.create` (which may have been
    removed in between) — the public/manual `revoke_invite` keeps its check. Bounded
    to that single row, and only while it is still unaccepted and unrevoked; an
    already-withdrawn invitation is left as it is. Audited as an `invite_revoked`
    platform event flagged `system`, attributed to the request that issued it.
    Raises if the row cannot be updated, so the caller can report the truth.
    """
    with transaction.atomic():
        # PLATFORM_QUERY_OK: tenants_invite is a tenancy root; the lookup is by the
        # primary key this same request just created, under a row lock.
        invite = Invite.objects.select_for_update().filter(pk=invite_id).first()
        if invite is None or invite.accepted_at or invite.revoked_at:
            return
        invite.revoked_at = timezone.now()
        invite.save(update_fields=["revoked_at"])
        record_platform_event(
            action=AuditAction.INVITE_REVOKED,
            origin=Origin(actor=requested_by, subject=invite.email),
            tenant_id=invite.tenant_id,
            metadata={
                "invite_id": str(invite.pk),
                "role": invite.role,
                "client_id": str(invite.client_id) if invite.client_id else None,
                "system": True,
                "reason": reason,
            },
        )


def assignment_state(client: ClientCompany) -> str:
    """Fingerprint the client's current assignments: their optimistic version.

    Assignments carry no version column of their own, so the SET is versioned: the
    page renders this digest, the service recomputes it under the client row lock,
    and a mismatch is a conflict. Replaying an old "add" after someone removed that
    person (or an old "remove" after a re-add) is refused instead of undoing the
    newer change.
    """
    rows = ClientAssignment.objects.filter(client=client).order_by("pk")
    material = ";".join(f"{row.pk}:{row.user_id}:{row.role}" for row in rows)
    # The counter makes the fingerprint monotonic (add then remove does not return
    # to an earlier value); the row digest also catches changes made elsewhere.
    digest = hashlib.sha256(material.encode()).hexdigest()[:48]
    return f"{client.assignment_version}:{digest}"


def _bump_assignment_version(client: ClientCompany) -> None:
    client.assignment_version += 1
    client.save(update_fields=["assignment_version"])


def _check_assignment_state(client: ClientCompany, expected_state: str) -> None:
    current = assignment_state(client)
    if current != expected_state:
        msg = f"client {client.pk} assignments changed since the page was rendered"
        raise ConflictError(msg)


def add_assignment(  # noqa: PLR0913 - keyword-only edit contract
    *,
    actor: User,
    tenant_id: UUID,
    client_id: UUID,
    user_id: UUID,
    expected_state: str,
    role: str = AssignmentRole.PRIMARY,
) -> ClientAssignment:
    """Put a firm member on a client; a repeat is a conflict, not a second row."""
    with transaction.atomic():
        client = load_client(actor, tenant_id, client_id, lock=True)
        _require(actor, ASSIGN_CAPABILITY, tenant_id)
        _check_assignment_state(client, expected_state)
        if ClientAssignment.objects.filter(client=client, user_id=user_id).exists():
            raise ValidationError(
                {"user": _("Esta pessoa já é responsável por este cliente.")},
            )
        user = User.objects.filter(pk=user_id).first()
        if user is None:
            raise ValidationError(
                {"user": _("Escolha alguém da equipe deste escritório.")},
            )
        assignment = assign_client(actor=actor, client=client, user=user, role=role)
        _bump_assignment_version(client)
        return assignment


def remove_assignment(
    *,
    actor: User,
    tenant_id: UUID,
    client_id: UUID,
    assignment_id: UUID,
    expected_state: str,
) -> None:
    """Take a firm member off a client; a vanished assignment is a conflict."""
    with transaction.atomic():
        client = load_client(actor, tenant_id, client_id, lock=True)
        _require(actor, ASSIGN_CAPABILITY, tenant_id)
        _check_assignment_state(client, expected_state)
        assignment = (
            ClientAssignment.objects.select_for_update()
            .filter(client=client, pk=assignment_id)
            .first()
        )
        if assignment is None:
            msg = "assignment no longer exists"
            raise ConflictError(msg)
        unassign_client(actor=actor, assignment=assignment)
        _bump_assignment_version(client)


__all__ = [
    "ASSIGN_CAPABILITY",
    "EDITABLE_FIELDS",
    "MANAGE_CAPABILITY",
    "ClientData",
    "ConflictError",
    "ContactData",
    "DuplicateClientError",
    "EngagementData",
    "InvitePendingError",
    "RetroNoteRequiredError",
    "add_assignment",
    "assignment_state",
    "create_client",
    "find_duplicate",
    "invite_contact",
    "load_client",
    "load_contact",
    "open_initial_claim",
    "remove_assignment",
    "save_contact",
    "save_engagement",
    "set_tax_profile",
    "update_client",
    "withdraw_undelivered_invite",
]
