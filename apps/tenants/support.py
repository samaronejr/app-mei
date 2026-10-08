"""Write side of support grants: request, approve, revoke (AXR1-023).

Three transactions, three authorities:

* `request_support_grant` — a platform staff account opens a request. It runs
  inside `tenant_context(tenant.pk)` *on the platform host* so the RLS insert
  predicate answers; the row admits nothing until an owner decides.
* `approve_support_grant` — the firm owner turns a pending row live, choosing
  scope, clients and duration. The service re-reads the grant `FOR NO KEY
  UPDATE` inside the same authority chain every account command uses: fresh
  actor, fresh tenant, owner seat.
* `revoke_support_grant` — the owner ends a live or pending grant. The decision
  is recorded on the row itself; a revoked grant cannot be re-activated.

Every transition writes BOTH audit streams: a `PlatformEvent` (who on the
platform side touched this grant) and a tenant `Event` (what the firm's own
log shows its owner). The owner reading the audit page sees exactly the
request, the approval and the revocation — never a silent platform session.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Final
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import QuerySet
from django.http import Http404
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditAction
from apps.audit.services import ObjectRef, Origin, record_event, record_platform_event
from apps.core.tenancy import tenant_context
from apps.tenants.models import Membership, Tenant, TenantRole
from apps.tenants.support_models import (
    MAX_SUPPORT_WINDOW,
    SupportGrant,
    SupportScope,
)

MAX_HOURS: Final = int(MAX_SUPPORT_WINDOW.total_seconds() // 3600)


@dataclass(frozen=True, slots=True)
class SupportApproval:
    """The owner's decision on a pending request."""

    scope: str
    client_ids: tuple[UUID, ...]
    hours: int


def _require_owner(actor: User, tenant_id: UUID) -> None:
    """Support decisions belong to the owner seat and to nothing else."""
    # PLATFORM_QUERY_OK: owner-seat existence on this exact tenant is the
    # authorization being checked; the actor is a firm member by definition.
    if not Membership.objects.filter(
        tenant_id=tenant_id,
        user=actor,
        client__isnull=True,
        is_active=True,
        role=TenantRole.OWNER,
    ).exists():
        msg = "Somente o responsável pela conta decide sobre suporte."
        raise PermissionDenied(msg)


def _grant_ref(grant: SupportGrant) -> ObjectRef:
    return ObjectRef("SupportGrant", str(grant.pk))


def _audit(
    action: str, *, actor: User, tenant: Tenant, grant: SupportGrant, **extra: object
) -> None:
    """One transition, both audit streams, same metadata."""
    metadata = {
        "grant_id": str(grant.pk),
        "requested_by": str(grant.requested_by_id),
        **extra,
    }
    record_platform_event(
        action=action,
        origin=Origin(actor=actor),
        tenant_id=tenant.pk,
        metadata=metadata,
    )
    record_event(action=action, actor=actor, obj=_grant_ref(grant), metadata=metadata)


@transaction.atomic
def request_support_grant(
    staff: User, tenant_id: UUID, *, purpose: str
) -> SupportGrant:
    """Open a pending request; admits nothing until an owner approves it.

    The `tenant_context` here is for the INSERT predicate alone — the platform
    actor has no session in this firm and this call is the reason there could
    ever be one. The requester must be `is_staff`: a firm member has no cause
    to request access they already hold, and granting a member path would make
    the owner approval page a door around the member's own role.
    """
    if not staff.is_active or not staff.is_staff:
        msg = "Somente operadores da plataforma solicitam acesso."
        raise PermissionDenied(msg)
    # PLATFORM_QUERY_OK: the staff requester picked this firm by id from their
    # own picker; the platform actor holds no firm seat to scope through.
    tenant = Tenant.objects.filter(pk=tenant_id, is_active=True).first()
    if tenant is None:
        raise Http404
    purpose = purpose.strip()
    if not purpose:
        msg = "Informe a finalidade do acesso."
        raise ValidationError(msg)
    with tenant_context(tenant.pk):
        grant, _created = SupportGrant.objects.get_or_create(
            tenant_id=tenant.pk,
            requested_by=staff,
            approved_by_owner__isnull=True,
            revoked_at__isnull=True,
            defaults={"purpose": purpose, "scope": SupportScope.READ_ONLY},
        )
        _audit(
            AuditAction.SUPPORT_GRANT_REQUESTED,
            actor=staff,
            tenant=tenant,
            grant=grant,
            purpose=purpose,
        )
    return grant


def pending_support_grants(tenant_id: UUID) -> QuerySet[SupportGrant]:
    """List this firm's undecided requests, newest first."""
    return (
        SupportGrant.objects.filter(
            tenant_id=tenant_id,
            approved_by_owner__isnull=True,
            revoked_at__isnull=True,
        )
        .select_related("requested_by")
        .order_by("-created_at")
    )


def decided_support_grants(tenant_id: UUID) -> QuerySet[SupportGrant]:
    """List this firm's decided grants — live, expired and revoked together."""
    return (
        SupportGrant.objects.filter(tenant_id=tenant_id)
        .exclude(
            approved_by_owner__isnull=True,
            revoked_at__isnull=True,
        )
        .select_related("requested_by", "approved_by_owner")
        .order_by("-approved_at", "-created_at")
    )


@transaction.atomic
def approve_support_grant(
    owner: User, tenant_id: UUID, grant_id: UUID, decision: SupportApproval
) -> SupportGrant:
    """Bind the owner, the firm, the grant and the rendered window in one write.

    `current_tenant_id` is checked the way `lock_firm_settings` checks it: an
    owner deciding for another firm's tenant id must 404, not decide.
    The grant is locked before it is re-read, so a request that raced to
    revocation between page render and POST is refused rather than revived.
    """
    from apps.core.tenancy import current_tenant_id  # noqa: PLC0415

    if current_tenant_id.get() != tenant_id:
        raise Http404
    if decision.hours < 1 or decision.hours > MAX_HOURS:
        msg = f"A duração deve ficar entre 1 e {MAX_HOURS} horas."
        raise ValidationError(msg)
    if decision.scope not in {value for value, _ in SupportScope.choices}:
        msg = "Escopo de acesso inválido."
        raise ValidationError(msg)
    _require_owner(owner, tenant_id)
    grant = (
        SupportGrant.objects.select_for_update(no_key=True)
        .filter(pk=grant_id, tenant_id=tenant_id)
        .first()
    )
    if grant is None:
        raise Http404
    if grant.approved_by_owner_id is not None or grant.revoked_at is not None:
        msg = "Esta solicitação já foi decidida."
        raise ValidationError(msg)
    now = timezone.now()
    grant.approved_by_owner = owner
    grant.approved_at = now
    grant.starts_at = now
    grant.expires_at = now + timedelta(hours=decision.hours)
    grant.scope = decision.scope
    grant.client_ids = [str(client_id) for client_id in decision.client_ids]
    grant.save()
    # PLATFORM_QUERY_OK: the grant row was already fetched tenant-scoped inside
    # tenant_context; this reload binds the audit event's tenant reference.
    tenant = Tenant.objects.get(pk=tenant_id)
    _audit(
        AuditAction.SUPPORT_GRANT_APPROVED,
        actor=owner,
        tenant=tenant,
        grant=grant,
        scope=grant.scope,
        hours=decision.hours,
        client_ids=grant.client_ids,
        expires_at=grant.expires_at.isoformat(),
    )
    return grant


@transaction.atomic
def revoke_support_grant(owner: User, tenant_id: UUID, grant_id: UUID) -> SupportGrant:
    """End a live or pending grant; the next support request dies with it.

    `revoked_at` doubles as the owner's *denial* of a pending request — a row
    either admits access or carries a decision; "declined" is a revocation that
    happened before approval. The middleware re-reads grant state per request,
    so this takes effect on the requester's very next page load.
    """
    from apps.core.tenancy import current_tenant_id  # noqa: PLC0415

    if current_tenant_id.get() != tenant_id:
        raise Http404
    _require_owner(owner, tenant_id)
    grant = (
        SupportGrant.objects.select_for_update(no_key=True)
        .filter(pk=grant_id, tenant_id=tenant_id)
        .first()
    )
    if grant is None:
        raise Http404
    if grant.revoked_at is not None:
        msg = "Esta concessão já está encerrada."
        raise ValidationError(msg)
    grant.revoked_at = timezone.now()
    grant.save(update_fields=["revoked_at", "updated_at"])
    # PLATFORM_QUERY_OK: the grant row was already fetched tenant-scoped inside
    # tenant_context; this reload binds the audit event's tenant reference.
    tenant = Tenant.objects.get(pk=tenant_id)
    _audit(
        AuditAction.SUPPORT_GRANT_REVOKED,
        actor=owner,
        tenant=tenant,
        grant=grant,
        requested_by=str(grant.requested_by_id),
    )
    return grant


__all__ = [
    "MAX_HOURS",
    "SupportApproval",
    "approve_support_grant",
    "decided_support_grants",
    "pending_support_grants",
    "request_support_grant",
    "revoke_support_grant",
]
