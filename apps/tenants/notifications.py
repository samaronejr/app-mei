"""Queue and authorize private firm notices without touching the mail transport."""

import hashlib
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils.timezone import localtime

from apps.accounts.invites import mint_token_for_dispatch
from apps.accounts.models import User
from apps.authz.firm_account import is_owner_invite
from apps.authz.portfolio import visible_clients
from apps.authz.services import can
from apps.core.tenancy import current_tenant_id
from apps.cycles.evidence import CLIENT_OPEN_STATES, EvidenceRequest
from apps.cycles.models import Exception as CycleException
from apps.cycles.models import ServiceCycle
from apps.cycles.services.cycle import OPEN_STATES
from apps.cycles.services.exceptions import ACTIVE_STATES
from apps.tenants.models import (
    FirmMessageKind,
    FirmOutboundMessage,
    Invite,
    Membership,
    NotificationPreference,
    Tenant,
)


def notice_key(
    tenant_id: UUID, subject_id: UUID, email: str, *, kind: str, period: date | None
) -> str:
    """Bind the intent to its exact recipient without persisting another address."""
    material = f"{tenant_id}:{kind}:{subject_id}:{period}:{email.strip().lower()}"
    return hashlib.sha256(material.encode()).hexdigest()


def enqueue_owner_invite(actor: User, invite: Invite) -> FirmOutboundMessage:
    """Queue the platform-created firm invitation; its token is never stored here."""
    if (
        current_tenant_id.get() != invite.tenant_id
        or not can(actor, "tenants.create")
        or not is_owner_invite(invite)
    ):
        raise PermissionDenied
    key = notice_key(
        invite.tenant_id,
        invite.pk,
        invite.email,
        kind=FirmMessageKind.OWNER_INVITE,
        period=None,
    )
    message, _created = FirmOutboundMessage.objects.get_or_create(
        idempotency_key=key,
        defaults={
            "tenant_id": invite.tenant_id,
            "kind": FirmMessageKind.OWNER_INVITE,
            "invite": invite,
            "created_by": actor,
        },
    )
    return message


def enqueue_summary(user: User, tenant: Tenant, period: date) -> FirmOutboundMessage:
    """Queue the active, opted-in member selected by the scheduler."""
    key = notice_key(
        tenant.pk,
        user.pk,
        user.email,
        kind=FirmMessageKind.DAILY_SUMMARY,
        period=period,
    )
    message, _created = FirmOutboundMessage.objects.get_or_create(
        idempotency_key=key,
        defaults={
            "tenant": tenant,
            "user": user,
            "period": period,
            "kind": FirmMessageKind.DAILY_SUMMARY,
        },
    )
    return message


@dataclass(frozen=True, slots=True)
class PreparedNotice:
    """Only immutable mail bytes and an address cross the transaction boundary."""

    recipient: str
    subject: str
    body: str

    @property
    def digest(self) -> str:
        """Bind the independent receipt to exactly what was handed to transport."""
        contents = f"{self.recipient}\0{self.subject}\0{self.body}"
        return hashlib.sha256(contents.encode()).hexdigest()


def prepare_invite(message: FirmOutboundMessage) -> PreparedNotice | None:
    """Recheck the operator, invitation and exact recipient immediately before send."""
    actor = message.created_by
    invite_id = message.invite_id
    if (
        actor is None
        or not actor.is_active
        or not actor.is_staff
        or not can(actor, "tenants.create")
        or not can(actor, "billing.manage")
        or invite_id is None
    ):
        return None
    invite = (
        # PLATFORM_QUERY_OK: this exact firm invitation is already bound by a paired FK.
        Invite.objects.select_for_update()
        .filter(pk=invite_id, tenant_id=message.tenant_id, client__isnull=True)
        .first()
    )
    if (
        invite is None
        or not is_owner_invite(invite)
        or notice_key(
            message.tenant_id,
            invite.pk,
            invite.email,
            kind=message.kind,
            period=None,
        )
        != message.idempotency_key
    ):
        return None
    token = mint_token_for_dispatch(invite)
    url = settings.PLATFORM_URL.rstrip("/") + reverse("invite-accept", args=[token])
    return PreparedNotice(
        invite.email,
        "Seu convite para administrar a firma",
        "Você recebeu um convite para administrar a conta da firma. "
        f"Para aceitar, acesse:\n{url}\n\n"
        f"O convite expira em {localtime(invite.expires_at):%d/%m/%Y %H:%M} (BRT).",
    )


def prepare_summary(
    message: FirmOutboundMessage, tenant: Tenant, *, on: date
) -> PreparedNotice | None:
    """Scope even aggregate counts to the recipient's current portfolio."""
    user = message.user
    if user is None or not user.is_active or message.period != on:
        return None
    if not can(user, "clients.view_assigned", tenant_id=tenant.pk):
        return None
    # PLATFORM_QUERY_OK: recipient eligibility is an exact active firm seat.
    if not Membership.objects.filter(
        tenant=tenant, user=user, client__isnull=True, is_active=True
    ).exists():
        return None
    if not NotificationPreference.objects.filter(
        user=user, daily_summary=True
    ).exists():
        return None
    if (
        notice_key(
            tenant.pk, user.pk, user.email, kind=message.kind, period=message.period
        )
        != message.idempotency_key
    ):
        return None
    clients = visible_clients(user, tenant_id=tenant.pk).values("pk")
    cycles = ServiceCycle.objects.filter(client_id__in=clients, state__in=OPEN_STATES)
    requests = EvidenceRequest.objects.filter(
        client_id__in=clients, state__in=CLIENT_OPEN_STATES
    )
    exceptions = CycleException.objects.filter(
        client_id__in=clients, state__in=ACTIVE_STATES
    )
    origin = urlsplit(settings.PLATFORM_URL)
    host = f"{tenant.slug}.{origin.hostname}"
    if origin.port:
        host = f"{host}:{origin.port}"
    home = urlunsplit((origin.scheme, host, "/", "", ""))
    body = (
        f"Resumo de {on:%d/%m/%Y} para a sua carteira:\n"
        f"Ciclos abertos: {cycles.count()}\n"
        f"Solicitações em aberto: {requests.count()}\n"
        f"Pendências: {exceptions.count()}\n\n"
        f"Acompanhe os detalhes: {home}\n"
        f"Preferências de e-mail: {home}configuracoes/preferencias/\n"
    )
    return PreparedNotice(user.email, "Resumo diário da sua carteira", body)
