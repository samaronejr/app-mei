"""Firm-side evidence-request commands (S6.3): locked, versioned, re-checked, evented.

Every command here takes the cycle row lock through `load_cycle` (which repeats the
portfolio scope, so an unassigned staff accountant gets 404 with nothing written),
then the request row lock, checks the `version` the form was rendered with, re-runs
`can()` inside the lock and writes one `CycleEvent` in the same transaction. A stale
form raises `VersionConflict` before any write.

Nothing here performs I/O. "Enviar solicitações ao cliente" and "Lembrar cliente"
only enqueue outbox rows (T10); the dispatcher sends them later, re-evaluating each
message against the live request before any network I/O (apps/delivery/kinds.py).
"""

from dataclasses import dataclass
from datetime import date
from typing import Final
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.utils import timezone

from apps.accounts.models import User
from apps.clients.models import ClientCompany, ClientContact
from apps.cycles.capabilities import CYCLES_OPERATE
from apps.cycles.errors import IllegalTransition
from apps.cycles.evidence import (
    CLIENT_OPEN_STATES,
    TITLE_MAX_LENGTH,
    EvidenceRequest,
    RequestInternal,
    RequestState,
    counted_links,
)
from apps.cycles.models import CycleEvent, ServiceCycle
from apps.cycles.services.cycle import (
    OPEN_STATES,
    check_version,
    load_cycle,
    require_actor,
    tenant_id,
)
from apps.delivery.notice_authority import NoticeOrigin, NoticeScope, lock_notice_scope
from apps.obligations.coverage import SERVICE_OBLIGATIONS
from apps.obligations.models import Obligation
from apps.tenants.models import Invite, Membership

NOTE_MAX_LENGTH: Final = 2000
# AXRM-012: a request is client-facing, so it may never name a firm-private obligation.
PRIVATE_OBLIGATION: Final = (
    "Esta obrigação é interna do escritório e não pode ser vinculada a uma "
    "solicitação ao cliente."
)
EDITABLE_STATES: Final = frozenset({RequestState.DRAFT, RequestState.REQUESTED})


@dataclass(frozen=True, slots=True)
class RequestFields:
    """What the firm types into the request form."""

    title: str
    client_note: str = ""
    due_on: date | None = None
    instructions: str = ""
    required: bool = True
    # T17: a `comprovante` request binds to the obligation it pays for.
    kind: str = "documento"
    obligation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class Recipient:
    """One contact as the send preview shows it.

    `access` is `portal` (holds an active seat in this client), `invite` (no seat, but
    a live portal invitation whose link the notice embeds) or `ineligible` (neither:
    flagged in the preview and never messaged).
    """

    contact: ClientContact
    access: str
    invite: Invite | None = None

    @property
    def eligible(self) -> bool:
        """Whether a message may be queued for this contact."""
        return self.access != "ineligible"


def _clean(fields: RequestFields) -> RequestFields:
    title = fields.title.strip()
    if not title or len(title) > TITLE_MAX_LENGTH:
        msg = "Informe um título de até 160 caracteres."
        raise ValidationError(msg)
    if len(fields.client_note) > NOTE_MAX_LENGTH or (
        len(fields.instructions) > NOTE_MAX_LENGTH
    ):
        msg = "Os textos da solicitação aceitam até 2.000 caracteres."
        raise ValidationError(msg)
    return RequestFields(
        title=title,
        client_note=fields.client_note.strip(),
        due_on=fields.due_on,
        instructions=fields.instructions.strip(),
        required=fields.required,
        kind=fields.kind,
        obligation_id=fields.obligation_id,
    )


def _bind_obligation(
    cycle: ServiceCycle,
    kind: str,
    obligation_id: UUID | None,
) -> Obligation | None:
    """Resolve the obligation a `comprovante` request pays for (T17).

    The binding is validated at write time inside the aggregate lock — not
    trusted from the form — and only while the request is a draft does the
    caller get to choose it. Returns the obligation row, or None for a
    `documento` request.
    """
    if kind == "documento":
        if obligation_id is not None:
            msg = "Uma solicitação de documento não vincula uma obrigação."
            raise ValidationError(msg)
        return None
    if kind != "comprovante":
        msg = "Tipo de solicitação desconhecido."
        raise ValidationError(msg)
    if obligation_id is None:
        msg = "Vincule a obrigação que este comprovante paga."
        raise ValidationError(msg)
    obligation = Obligation.objects.filter(pk=obligation_id).first()
    if (
        obligation is None
        or obligation.client_id != cycle.client_id
        or obligation.competence_month != cycle.period_start
    ):
        raise Http404
    if not obligation.portal_visible:
        raise ValidationError(PRIVATE_OBLIGATION)
    engagement = cycle.engagement
    if engagement.starts_on > cycle.period_start or (
        engagement.ends_on is not None and cycle.period_start >= engagement.ends_on
    ):
        msg = "Este serviço não se aplica à competência desta solicitação."
        raise ValidationError(msg)
    codes = SERVICE_OBLIGATIONS.get(engagement.service_kind)
    if not codes or obligation.obligation_type_id not in codes:
        msg = "Este serviço não tem uma obrigação mapeada para esta solicitação."
        raise ValidationError(msg)
    return obligation


def _event(
    cycle: ServiceCycle,
    actor: User,
    event_type: str,
    request: EvidenceRequest,
    payload: dict[str, str | int] | None = None,
) -> CycleEvent:
    """Ids and codes only: request titles and notes never enter the append-only log."""
    return CycleEvent.objects.create(
        tenant_id=cycle.tenant_id,
        client_id=cycle.client_id,
        cycle=cycle,
        actor=actor,
        type=event_type,
        aggregate_type="EvidenceRequest",
        aggregate_id=request.pk,
        aggregate_version=request.version,
        payload=payload or {},
    )


def _open_cycle(actor: User, cycle_id: UUID) -> ServiceCycle:
    cycle = load_cycle(actor, cycle_id, lock=True)
    if cycle.state not in OPEN_STATES:
        msg = "Este ciclo está encerrado."
        raise IllegalTransition(msg)
    return cycle


def _locked_request(
    actor: User, request_id: UUID
) -> tuple[ServiceCycle, EvidenceRequest]:
    """Portfolio-scope through the cycle first, then lock the request row."""
    candidate = EvidenceRequest.objects.filter(pk=request_id).first()
    if candidate is None:
        raise Http404
    cycle = _open_cycle(actor, candidate.cycle_id)
    request = EvidenceRequest.objects.select_for_update().get(pk=candidate.pk)
    return cycle, request


def create_request(
    actor: User,
    cycle_id: UUID,
    fields: RequestFields,
    *,
    expected_version: int,
) -> EvidenceRequest:
    """Add a draft request to a cycle, against the cycle revision the form carried."""
    cleaned = _clean(fields)
    with transaction.atomic():
        cycle = _open_cycle(actor, cycle_id)
        check_version(cycle.version, expected_version)
        require_actor(actor, CYCLES_OPERATE)
        obligation = _bind_obligation(cycle, cleaned.kind, cleaned.obligation_id)
        request = EvidenceRequest.objects.create(
            tenant_id=cycle.tenant_id,
            client_id=cycle.client_id,
            cycle=cycle,
            title=cleaned.title,
            client_note=cleaned.client_note,
            due_on=cleaned.due_on,
            required=cleaned.required,
            kind=cleaned.kind,
            obligation=obligation,
            created_by=actor,
        )
        RequestInternal.objects.create(
            tenant_id=cycle.tenant_id,
            client_id=cycle.client_id,
            request=request,
            instructions=cleaned.instructions,
        )
        cycle.version += 1
        cycle.save(update_fields=["version"])
        _event(cycle, actor, "request.created", request)
        return request


def update_request(
    actor: User,
    request_id: UUID,
    fields: RequestFields,
    *,
    expected_version: int,
) -> EvidenceRequest:
    """Edit a draft or an outstanding request; a replayed old form is refused."""
    cleaned = _clean(fields)
    with transaction.atomic():
        cycle, request = _locked_request(actor, request_id)
        check_version(request.version, expected_version)
        require_actor(actor, CYCLES_OPERATE)
        if request.state not in EDITABLE_STATES:
            msg = "Esta solicitação não pode mais ser editada."
            raise IllegalTransition(msg)
        # The bound row is needed for the non-draft identity check below; it is
        # also the object the draft rebind writes, so it loads once here.
        request.obligation = (
            Obligation.objects.filter(pk=request.obligation_id).first()
            if request.obligation_id
            else None
        )
        if request.state == RequestState.DRAFT:
            request.obligation = _bind_obligation(
                cycle, cleaned.kind, cleaned.obligation_id
            )
            request.kind = cleaned.kind
        elif (
            cleaned.kind != request.kind
            or cleaned.obligation_id != request.obligation_id
        ):
            msg = "Tipo e obrigação vinculada só mudam em rascunho."
            raise ValidationError(msg)
        request.title = cleaned.title
        request.client_note = cleaned.client_note
        request.due_on = cleaned.due_on
        request.required = cleaned.required
        request.version += 1
        request.save(
            update_fields=[
                "title",
                "client_note",
                "due_on",
                "required",
                "kind",
                "obligation",
                "version",
            ]
        )
        internal = RequestInternal.objects.select_for_update().get(request=request)
        internal.instructions = cleaned.instructions
        internal.version += 1
        internal.save(update_fields=["instructions", "version"])
        _event(cycle, actor, "request.updated", request)
        return request


def waive_request(
    actor: User,
    request_id: UUID,
    *,
    reason: str,
    expected_version: int,
) -> EvidenceRequest:
    """Dispense a request with a firm-only reason (S6.3 `dispensado`)."""
    if not reason.strip() or len(reason) > NOTE_MAX_LENGTH:
        msg = "Informe o motivo da dispensa (até 2.000 caracteres)."
        raise ValidationError(msg)
    with transaction.atomic():
        cycle, request = _locked_request(actor, request_id)
        check_version(request.version, expected_version)
        require_actor(actor, CYCLES_OPERATE)
        if request.state in {
            RequestState.WAIVED,
            RequestState.ACCEPTED,
            RequestState.REPLACED,
        }:
            msg = "Esta solicitação já foi encerrada."
            raise IllegalTransition(msg)
        previous = request.state
        request.state = RequestState.WAIVED
        request.version += 1
        request.save(update_fields=["state", "version"])
        internal = RequestInternal.objects.select_for_update().get(request=request)
        internal.waiver_reason = reason.strip()
        internal.version += 1
        internal.save(update_fields=["waiver_reason", "version"])
        _event(
            cycle,
            actor,
            "request.waived",
            request,
            {"from": previous, "to": RequestState.WAIVED},
        )
        return request


def _has_seat(contact: ClientContact, client: ClientCompany) -> bool:
    if contact.membership_id is not None:
        # PLATFORM_QUERY_OK: tenants_membership is a tenancy root; this reads the one
        # seat the contact row names and re-checks it belongs to this client.
        seat = Membership.objects.filter(
            pk=contact.membership_id,
            tenant_id=client.tenant_id,
            client_id=client.pk,
            is_active=True,
        )
        if seat.exists():
            return True
    # PLATFORM_QUERY_OK: the same tenancy root, confined to this firm and client, for
    # a contact whose seat was granted through an accepted invitation.
    return Membership.objects.filter(
        tenant_id=client.tenant_id,
        client_id=client.pk,
        is_active=True,
        user__email__iexact=contact.email,
    ).exists()


def live_invite(contact: ClientContact, client: ClientCompany) -> Invite | None:
    """Return the newest unexpired, unrevoked, unaccepted invite for this contact."""
    return (
        # PLATFORM_QUERY_OK: tenants_invite has no row-level policy; confined to
        # this firm, this client and this contact's address.
        Invite.objects.filter(
            tenant_id=client.tenant_id,
            client_id=client.pk,
            email__iexact=contact.email,
            accepted_at__isnull=True,
            revoked_at__isnull=True,
            expires_at__gt=timezone.now(),
        )
        .order_by("-created_at")
        .first()
    )


def recipients(client: ClientCompany) -> list[Recipient]:
    """Classify every active, e-mailable contact of the client for the preview."""
    rows = []
    contacts = ClientContact.objects.filter(
        client=client, active=True, channel_email_allowed=True
    ).exclude(email="")
    for contact in contacts.order_by("name", "pk"):
        if _has_seat(contact, client):
            rows.append(Recipient(contact, "portal"))
            continue
        invite = live_invite(contact, client)
        rows.append(
            Recipient(contact, "invite", invite)
            if invite is not None
            else Recipient(contact, "ineligible")
        )
    return rows


def preview_recipients(actor: User, cycle_id: UUID) -> list[Recipient]:
    """Read-only preview, portfolio-scoped like every other cycle read."""
    cycle = load_cycle(actor, cycle_id)
    client = ClientCompany.objects.get(pk=cycle.client_id)
    return recipients(client)


def _enqueue_for(  # noqa: PLR0913 - one message per recipient needs every key part
    actor: User,
    cycle: ServiceCycle,
    recipient: Recipient,
    *,
    kind: str,
    occurrence: int,
    notice_scope: NoticeScope,
    evidence_request: EvidenceRequest | None = None,
) -> None:
    # Delivery depends on cycles, so the reverse import waits until call time.
    from apps.delivery.outbox import enqueue  # noqa: PLC0415
    from apps.delivery.policy import BusinessIntent  # noqa: PLC0415

    subject = evidence_request.pk if evidence_request is not None else cycle.pk
    # The client revision is read under the client lock this transaction already
    # holds (load_cycle -> load_client, re-taken by enqueue), so it cannot be stale:
    # the command's own optimistic check is the cycle/request version above.
    current = ClientCompany.objects.get(pk=cycle.client_id).version
    enqueue(
        actor,
        cycle.client_id,
        recipient.contact.pk,
        intent=BusinessIntent(
            cycle.engagement_id,
            cycle.playbook_id,
            cycle.period_start,
            cycle.period_end,
            cycle.amendment,
            f"{kind}:{subject}:{recipient.contact.pk}",
            kind,
            occurrence,
        ),
        expected_version=current,
        kind=kind,
        cycle_id=cycle.pk,
        invite=recipient.invite,
        evidence_request=evidence_request,
        notice_scope=notice_scope,
    )


def send_requests(
    actor: User,
    cycle_id: UUID,
    *,
    expected_version: int,
) -> list[Recipient]:
    """Publish every draft request to the portal and queue one notice per recipient.

    Refused, with nothing written, when no draft exists or no contact can receive the
    notice (a contact with neither a seat nor a live invitation is flagged in the
    preview, never messaged).
    """
    with transaction.atomic():
        candidate = load_cycle(actor, cycle_id)
        scope = lock_notice_scope(
            actor,
            NoticeOrigin("request_notice", candidate.client_id, candidate.pk),
        )
        actor = scope.actor
        cycle = _open_cycle(actor, cycle_id)
        check_version(cycle.version, expected_version)
        require_actor(actor, CYCLES_OPERATE)
        drafts = list(
            EvidenceRequest.objects.select_for_update()
            .filter(cycle=cycle, state=RequestState.DRAFT)
            .order_by("created_at", "pk")
        )
        if not drafts:
            msg = "Não há solicitações em rascunho para enviar."
            raise ValidationError(msg)
        if EvidenceRequest.objects.filter(
            pk__in=[draft.pk for draft in drafts], obligation__portal_visible=False
        ).exists():
            raise ValidationError(PRIVATE_OBLIGATION)
        client = ClientCompany.objects.get(pk=cycle.client_id)
        eligible = [row for row in recipients(client) if row.eligible]
        if not eligible:
            msg = (
                "Nenhum contato tem acesso ao portal ou convite pendente. "
                "Convide um contato antes de enviar."
            )
            raise ValidationError(msg)
        now = timezone.now()
        for request in drafts:
            request.state = RequestState.REQUESTED
            request.requested_at = now
            request.version += 1
            request.save(update_fields=["state", "requested_at", "version"])
            _event(
                cycle,
                actor,
                "request.sent",
                request,
                {"from": RequestState.DRAFT, "to": RequestState.REQUESTED},
            )
        cycle.version += 1
        cycle.save(update_fields=["version"])
        for recipient in eligible:
            _enqueue_for(
                actor,
                cycle,
                recipient,
                kind="request_notice",
                occurrence=cycle.version,
                notice_scope=scope,
            )
        return eligible


def remind_request(
    actor: User,
    request_id: UUID,
    *,
    expected_version: int,
) -> list[Recipient]:
    """Queue a reminder about one outstanding request; re-checked again at send."""
    with transaction.atomic():
        candidate = EvidenceRequest.objects.filter(pk=request_id).first()
        if candidate is None:
            raise Http404
        scope = lock_notice_scope(
            actor,
            NoticeOrigin(
                "request_reminder",
                candidate.client_id,
                candidate.cycle_id,
                candidate.pk,
            ),
        )
        actor = scope.actor
        cycle, request = _locked_request(actor, request_id)
        check_version(request.version, expected_version)
        require_actor(actor, CYCLES_OPERATE)
        if (
            request.state not in CLIENT_OPEN_STATES
            or counted_links().filter(request=request).exists()
        ):
            msg = "Esta solicitação não está aguardando o cliente."
            raise IllegalTransition(msg)
        client = ClientCompany.objects.get(pk=cycle.client_id)
        eligible = [row for row in recipients(client) if row.eligible]
        if not eligible:
            msg = "Nenhum contato pode receber o lembrete."
            raise ValidationError(msg)
        request.version += 1
        request.save(update_fields=["version"])
        _event(cycle, actor, "request.reminded", request)
        for recipient in eligible:
            _enqueue_for(
                actor,
                cycle,
                recipient,
                kind="request_reminder",
                occurrence=request.version,
                notice_scope=scope,
                evidence_request=request,
            )
        return eligible


def client_status(request: EvidenceRequest, *, submitted: bool, today: date) -> str:
    """Derive what the client-facing chip says; never stored (S8.2)."""
    if submitted and request.state in CLIENT_OPEN_STATES:
        return "recebido"
    if (
        request.state in CLIENT_OPEN_STATES
        and request.due_on
        and request.due_on < today
    ):
        return "atrasada"
    return str(request.state)


__all__ = [
    "Recipient",
    "RequestFields",
    "client_status",
    "create_request",
    "live_invite",
    "preview_recipients",
    "recipients",
    "remind_request",
    "send_requests",
    "tenant_id",
    "update_request",
    "waive_request",
]
