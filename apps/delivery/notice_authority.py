"""Exact manual notice authority, independent from immutable message content."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.services import ObjectRef, record_event
from apps.authz.approvals import (
    Authority,
    is_policy_owner,
    lock_authorities,
    may_configure_policy,
)
from apps.authz.services import can
from apps.authz.support import in_support_mode
from apps.clients.maintenance import load_client
from apps.clients.models import ClientAssignment, ClientCompany, ClientContact
from apps.core.flags import tenant_row_has
from apps.core.tenancy import current_tenant_id
from apps.cycles.bulk_contracts import (
    integer_value,
    list_value,
    object_value,
    text_value,
)
from apps.cycles.capabilities import CYCLES_OPERATE
from apps.cycles.effects.manifest import JSONValue, canonicalize
from apps.cycles.evidence import EvidenceRequest
from apps.cycles.models import CycleEvent, PlaybookVersion, ServiceCycle, VersionState
from apps.delivery.models import DeliveryAttempt, OutboundMessage, TenantNoticePolicy
from apps.delivery.retries import root_intent as retry_root_intent
from apps.tenants.account_state import require_writable
from apps.tenants.models import Invite, Tenant

AUTOMATIONS_MANAGE: Final = "automations.manage"
ACKNOWLEDGEMENT: Final = "notice-policy.manual.v1"
SCHEMA: Final = "axorim.notice-authorization.v1"
NOTICE_CHOICES: Final = (
    ("notice", "Aviso simples por e-mail"),
    ("portal_invite", "Convite ao portal enviado pela ficha do cliente"),
    ("contact_portal_invite", "Convite ao portal enviado pela lista de contatos"),
    ("request_notice", "Solicitação de documentos do ciclo"),
    ("request_reminder", "Lembrete manual de documento pendente"),
)
NOTICE_KINDS: Final = frozenset(kind for kind, _ in NOTICE_CHOICES)


class NoticeRefusalError(ValidationError):
    """A machine-readable, side-effect-free refusal before credential creation."""

    def __init__(self, code: str) -> None:
        """Keep the stable reason code separate from the rendered refusal."""
        self.reason = code
        super().__init__(
            "O aviso não tem autorização vigente. O titular pode revisar a "
            "política de avisos; ciclos com playbook publicado "
            "usam o próprio playbook.",
            code=code,
        )


def tenant_id() -> UUID:
    """Require the exact current firm boundary."""
    value = current_tenant_id.get()
    if value is None:
        raise Http404
    return UUID(str(value))


def enabled(tenant: Tenant) -> bool:
    """Expose management when either supported firm origin is available."""
    return tenant_row_has(tenant, "cycles_r1") or tenant_row_has(tenant, "import_r1")


def may_manage(user: User, tenant: Tenant) -> bool:
    """Navigation only; the command repeats admission under fresh row fences."""
    return (
        enabled(tenant)
        and not in_support_mode()
        and may_configure_policy(user, tenant.pk)
        and can(user, AUTOMATIONS_MANAGE, tenant_id=tenant.pk)
    )


def _grants(kinds: tuple[str, ...]) -> list[JSONValue]:
    if len(set(kinds)) != len(kinds) or not set(kinds) <= NOTICE_KINDS:
        msg = "Tipo de aviso indisponível."
        raise ValidationError(msg)
    return [
        {"kind": kind, "mode": "manual", "rule_version": None} for kind in sorted(kinds)
    ]


def granted_kinds(policy: TenantNoticePolicy | None) -> tuple[str, ...]:
    """Read only the finite canonical manual grant set."""
    if policy is None:
        return ()
    values = list_value(policy.grants_json)
    kinds = tuple(text_value(object_value(value).get("kind")) for value in values)
    if canonicalize(values) != canonicalize(_grants(kinds)):
        msg = "notice_policy_invalid"
        raise NoticeRefusalError(msg)
    return kinds


def _owner(authority: Authority, tenant: Tenant) -> bool:
    return (
        enabled(tenant)
        and not in_support_mode()
        and is_policy_owner(authority)
        and can(authority.user, AUTOMATIONS_MANAGE, tenant_id=tenant.pk)
    )


def configure(
    actor: User,
    *,
    kinds: tuple[str, ...],
    acknowledged: bool,
    expected_version: int,
) -> TenantNoticePolicy:
    """Only a fresh ordinary owner may acknowledge exact implemented grants."""
    grants = _grants(kinds)
    if grants and not acknowledged:
        msg = "Confirme a autorização antes de salvar os avisos."
        raise ValidationError(msg)
    with transaction.atomic():
        authority = lock_authorities(
            {actor.pk},
            tenant_id=tenant_id(),
            extra_capabilities=(AUTOMATIONS_MANAGE,),
        )[actor.pk]
        # PLATFORM_QUERY_OK: current tenant, fenced with the acknowledging owner.
        tenant = Tenant.objects.select_for_update(no_key=True).get(pk=tenant_id())
        if not _owner(authority, tenant):
            raise PermissionDenied
        require_writable(tenant)
        policy = TenantNoticePolicy.objects.select_for_update().filter().first()
        if (policy.version if policy else 0) != expected_version:
            msg = "notice_policy_changed"
            raise NoticeRefusalError(msg)
        if policy is None:
            policy = TenantNoticePolicy(tenant_id=tenant.pk)
        else:
            policy.version += 1
        policy.grants_json = grants
        member = authority.membership
        if grants:
            if member is None:
                raise PermissionDenied
            policy.acknowledged_version = policy.version
            policy.acknowledged_by = authority.user
            policy.ack_membership_id = member.pk
            policy.ack_membership_version = member.version
            policy.acknowledged_at = timezone.now()
            policy.acknowledgement_code = ACKNOWLEDGEMENT
        else:
            policy.acknowledged_version = None
            policy.acknowledged_by = None
            policy.ack_membership_id = None
            policy.ack_membership_version = None
            policy.acknowledged_at = None
            policy.acknowledgement_code = ""
        policy.updated_at = timezone.now()
        policy.full_clean(exclude=["tenant", "acknowledged_by"])
        policy.save()
        record_event(
            actor=authority.user,
            tenant_id=tenant.pk,
            action="delivery.notice_policy_changed",
            obj=ObjectRef("delivery.TenantNoticePolicy", str(policy.pk)),
            metadata={
                "version": policy.version,
                "grants": grants,
                "acknowledged": bool(grants),
                "acknowledged_by_id": str(policy.acknowledged_by_id)
                if policy.acknowledged_by_id
                else None,
                "ack_membership_id": str(policy.ack_membership_id)
                if policy.ack_membership_id
                else None,
                "ack_membership_version": policy.ack_membership_version,
                "acknowledgement_code": policy.acknowledgement_code,
                "acknowledged_at": policy.acknowledged_at.isoformat()
                if policy.acknowledged_at
                else None,
            },
        )
        return policy


@dataclass(frozen=True, slots=True)
class NoticeScope:
    """All participants and sources are fenced before a producer's first write."""

    actor: User
    client: ClientCompany
    cycle: ServiceCycle | None
    kind: str
    issuer_membership_id: UUID
    issuer_membership_version: int
    basis: dict[str, JSONValue]
    authorities: dict[UUID, Authority]


@dataclass(frozen=True, slots=True)
class NoticeOrigin:
    """Name the exact existing notice sources, without authority or transport."""

    kind: str
    client_id: UUID
    cycle_id: UUID | None = None
    request_id: UUID | None = None


def _policy_basis(
    policy: TenantNoticePolicy | None,
    authority: Authority | None,
    tenant: Tenant,
    kind: str,
) -> dict[str, JSONValue]:
    if policy is None:
        msg = "notice_policy_missing"
        raise NoticeRefusalError(msg)
    if (
        policy.acknowledged_by_id is None
        or policy.acknowledged_at is None
        or policy.acknowledged_version != policy.version
        or policy.acknowledgement_code != ACKNOWLEDGEMENT
    ):
        msg = "notice_policy_unacknowledged"
        raise NoticeRefusalError(msg)
    if kind not in granted_kinds(policy):
        msg = "notice_kind_not_allowed"
        raise NoticeRefusalError(msg)
    if (
        authority is None
        or not _owner(authority, tenant)
        or authority.user.pk != policy.acknowledged_by_id
        or authority.membership is None
        or authority.membership.pk != policy.ack_membership_id
        or authority.membership.version != policy.ack_membership_version
    ):
        msg = "notice_owner_changed"
        raise NoticeRefusalError(msg)
    return {
        "type": "tenant_policy",
        "policy_id": str(policy.pk),
        "policy_version": policy.version,
        "owner_id": str(authority.user.pk),
        "membership_id": str(authority.membership.pk),
        "membership_version": authority.membership.version,
        "acknowledgement_code": ACKNOWLEDGEMENT,
    }


def _definition_digest(definition: PlaybookVersion) -> str:
    """Hash the implemented immutable definition, not its retirement counter."""
    steps: list[JSONValue] = [
        {
            "key": step.key,
            "position": step.position,
            "kind": step.kind,
            "label": step.label_ptbr,
            "client_label": step.client_label_ptbr,
            "depends_on": step.depends_on,
            "owner_rule": step.owner_rule,
            "due_rule": step.due_rule_json,
            "evidence_required": step.evidence_required,
            "client_visible": step.client_visible,
            "enabled": step.is_enabled,
        }
        for step in definition.steps.order_by("key")
    ]
    raw = canonicalize(
        {
            "schema": 1,
            "number": definition.number,
            "applies_to": definition.applies_to_json,
            "steps": steps,
        }
    )
    return hashlib.sha256(raw).hexdigest()


def _lock_sources(
    origin: NoticeOrigin, authority: Authority, tid: UUID
) -> tuple[ClientCompany, ServiceCycle | None, dict[str, JSONValue] | None]:
    """Acquire client -> assignments -> cycle/requests -> published definition."""
    client = load_client(authority.user, tid, origin.client_id, lock=True)
    if not _issuer_permits(authority, origin, tid):
        raise PermissionDenied
    # Stable portfolio/assignment decisions share the client aggregate fence.
    list(
        ClientAssignment.objects.select_for_update()
        .filter(client=client)
        .order_by("pk")
    )
    cycle = None
    if origin.cycle_id is not None:
        cycle = (
            ServiceCycle.objects.select_for_update(no_key=True)
            .filter(pk=origin.cycle_id, client=client)
            .first()
        )
        if cycle is None:
            msg = "notice_source_changed"
            raise NoticeRefusalError(msg)
        list(
            EvidenceRequest.objects.select_for_update()
            .filter(cycle=cycle, client=client)
            .order_by("pk")
        )
    if origin.request_id is not None and (
        cycle is None
        or not EvidenceRequest.objects.filter(
            pk=origin.request_id, client=client, cycle=cycle
        ).exists()
    ):
        msg = "notice_source_changed"
        raise NoticeRefusalError(msg)
    published_basis: dict[str, JSONValue] | None = None
    if cycle is not None:
        definition = (
            PlaybookVersion.objects.select_for_update(no_key=True)
            .filter(
                pk=cycle.playbook_version_id,
                playbook_id=cycle.playbook_id,
                state__in=[
                    VersionState.PUBLISHED,
                    VersionState.RETIRED,
                ],
            )
            .first()
        )
        if definition is None or definition.published_at is None:
            msg = "notice_source_changed"
            raise NoticeRefusalError(msg)
        published_basis = {
            "type": "playbook",
            "playbook_id": str(cycle.playbook_id),
            "playbook_version_id": str(definition.pk),
            "definition_digest": _definition_digest(definition),
            "schema_version": 1,
            "published_at": definition.published_at.isoformat(),
            "effect_revision": cycle.effect_revision,
        }
    return client, cycle, published_basis


def _issuer_permits(authority: Authority, origin: NoticeOrigin, tid: UUID) -> bool:
    return authority.permits(CYCLES_OPERATE, origin.client_id, tid) and (
        origin.kind not in {"portal_invite", "contact_portal_invite"}
        or authority.permits("users.create", origin.client_id, tid)
    )


def lock_notice_scope(
    actor: User,
    origin: NoticeOrigin,
    *,
    binding: dict[str, JSONValue] | None = None,
    extra_actor: User | None = None,
) -> NoticeScope:
    """Users -> seats/grants -> tenant/policy -> client/cycle -> contact/invite."""
    if origin.kind not in NOTICE_KINDS:
        msg = "notice_kind_not_allowed"
        raise NoticeRefusalError(msg)
    tid = tenant_id()
    policy_required = origin.kind in {"portal_invite", "contact_portal_invite"} or (
        object_value(binding.get("basis")).get("type") == "tenant_policy"
        if binding is not None
        else origin.cycle_id is None
    )
    candidate = TenantNoticePolicy.objects.filter().first() if policy_required else None
    owner_id = (
        UUID(text_value(object_value(binding.get("basis")).get("owner_id")))
        if binding is not None and policy_required
        else candidate.acknowledged_by_id
        if candidate
        else None
    )
    participants = {actor.pk}
    participants.update({owner_id} if owner_id else set())
    participants.update({extra_actor.pk} if extra_actor else set())
    authorities = lock_authorities(
        participants,
        tenant_id=tid,
        extra_capabilities=(AUTOMATIONS_MANAGE, "users.create"),
    )
    authority = authorities[actor.pk]
    if authority.membership is None:
        raise PermissionDenied
    # PLATFORM_QUERY_OK: current tenant fence, after the full participant union.
    tenant = Tenant.objects.select_for_update(no_key=True).get(pk=tid)
    feature = "import_r1" if origin.kind == "contact_portal_invite" else "cycles_r1"
    if not tenant_row_has(tenant, feature):
        msg = "notice_feature_disabled"
        raise NoticeRefusalError(msg)
    require_writable(tenant)
    policy = (
        TenantNoticePolicy.objects.select_for_update().filter().first()
        if policy_required
        else None
    )
    if policy and policy.acknowledged_by_id not in {owner_id, None}:
        msg = "notice_owner_changed"
        raise NoticeRefusalError(msg)
    client, cycle, published_basis = _lock_sources(origin, authority, tid)
    owner = authorities.get(owner_id) if owner_id else None
    if policy_required:
        basis = _policy_basis(policy, owner, tenant, origin.kind)
    elif published_basis is not None:
        basis = published_basis
    else:
        msg = "notice_source_changed"
        raise NoticeRefusalError(msg)
    if binding is not None and canonicalize(
        object_value(binding.get("basis"))
    ) != canonicalize(basis):
        code = (
            "notice_policy_changed"
            if object_value(binding.get("basis")).get("type") == "tenant_policy"
            else "notice_playbook_changed"
        )
        raise NoticeRefusalError(code)
    list(ClientContact.objects.select_for_update().filter(client=client).order_by("pk"))
    list(
        # PLATFORM_QUERY_OK: client invitations are tenant/client fenced; FIRM excluded.
        Invite.objects.select_for_update()
        .filter(tenant_id=tid, client_id=client.pk)
        .order_by("pk")
    )
    # Refresh time-dependent grants after any blocked source acquisition.
    if not _issuer_permits(authority, origin, tid):
        raise PermissionDenied
    if policy_required:
        _policy_basis(policy, owner, tenant, origin.kind)
    return NoticeScope(
        authority.user,
        client,
        cycle,
        origin.kind,
        authority.membership.pk,
        authority.membership.version,
        basis,
        authorities,
    )


def _envelope(
    scope: NoticeScope, message: OutboundMessage, *, root_intent: str
) -> dict[str, JSONValue]:
    if (
        scope.kind != message.kind
        or scope.client.pk != message.client_id
        or (scope.cycle.pk if scope.cycle else None) != message.cycle_id
        or scope.actor.pk != message.created_by_id
    ):
        msg = "notice_source_changed"
        raise NoticeRefusalError(msg)
    if not ClientContact.objects.filter(
        pk=message.contact_id,
        client_id=message.client_id,
    ).exists():
        msg = "contact_unavailable"
        raise NoticeRefusalError(msg)
    if message.invite_id is not None:
        # PLATFORM_QUERY_OK: exact source invitation, already locked by scope.
        invite = Invite.objects.filter(
            pk=message.invite_id,
            tenant_id=message.tenant_id,
            client_id=message.client_id,
        ).first()
        if invite is None or (
            invite.contact_id is not None and invite.contact_id != message.contact_id
        ):
            msg = "notice_source_changed"
            raise NoticeRefusalError(msg)
    return {
        "schema": SCHEMA,
        "feature": "import_r1"
        if message.kind == "contact_portal_invite"
        else "cycles_r1",
        "mode": "manual",
        "kind": message.kind,
        "tenant_id": str(message.tenant_id),
        "client_id": str(message.client_id),
        "contact_id": str(message.contact_id),
        "cycle_id": str(message.cycle_id) if message.cycle_id else None,
        "request_id": str(message.evidence_request_id)
        if message.evidence_request_id
        else None,
        "invite_id": str(message.invite_id) if message.invite_id else None,
        "issuer_id": str(scope.actor.pk),
        "issuer_membership_id": str(scope.issuer_membership_id),
        "issuer_membership_version": scope.issuer_membership_version,
        "content_digest": message.content_digest,
        "root_intent": root_intent,
        "basis": scope.basis,
    }


def bind_notice(scope: NoticeScope, message: OutboundMessage) -> None:
    """Bind a new row while the producer still holds the complete scope."""
    data = _envelope(scope, message, root_intent=message.business_intent_key)
    raw = canonicalize(data)
    message.notice_authorization_bytes = raw
    message.notice_authorization_digest = hashlib.sha256(raw).digest()


def _legacy_pin(cycle: ServiceCycle, created_at: datetime) -> str:
    """Resolve only an unambiguous opening and contiguous pre-enqueue pin chain."""
    events = list(
        CycleEvent.objects.filter(
            cycle=cycle,
            aggregate_type="ServiceCycle",
            aggregate_id=cycle.pk,
            type__in=["cycle.opened", "cycle.playbook_migrated"],
        ).order_by("occurred_at", "pk")
    )
    opened = [event for event in events if event.type == "cycle.opened"]
    if len(opened) != 1 or opened[0].occurred_at > created_at:
        return ""
    payload = object_value(opened[0].payload)
    pin = text_value(payload.get("playbook_version_id"))
    seen_times: set[datetime] = set()
    for event in events:
        if event.occurred_at in seen_times:
            return ""
        seen_times.add(event.occurred_at)
        if event.type != "cycle.playbook_migrated":
            continue
        if event.occurred_at > created_at:
            return ""
        payload = object_value(event.payload)
        if text_value(payload.get("from")) != pin:
            return ""
        pin = text_value(payload.get("to"))
    return pin


def _legacy_publication(message: OutboundMessage, scope: NoticeScope) -> bool:
    """Prove service publication without filling or inferring policy consent."""
    cycle = scope.cycle
    if (
        message.kind not in {"notice", "request_notice", "request_reminder"}
        or cycle is None
        or scope.basis.get("type") != "playbook"
        or datetime.fromisoformat(text_value(scope.basis.get("published_at")))
        > message.created_at
    ):
        return False
    if _legacy_pin(cycle, message.created_at) != str(cycle.playbook_version_id):
        return False
    return (
        message.created_by_id is not None
        and DeliveryAttempt.objects.filter(
            message=message, error_code="enqueued", actor_id=message.created_by_id
        ).exists()
        and CycleEvent.objects.filter(
            cycle=cycle,
            aggregate_type="OutboundMessage",
            aggregate_id=message.pk,
            type="delivery.observed",
            actor_id=message.created_by_id,
            payload__code="enqueued",
        ).exists()
    )


def _read_binding(message: OutboundMessage) -> dict[str, JSONValue] | None:
    raw = message.notice_authorization_bytes
    if raw is None:
        if message.kind in {"portal_invite", "contact_portal_invite"} or (
            message.cycle_id is None
        ):
            msg = "notice_policy_missing"
            raise NoticeRefusalError(msg)
        return None
    binding = object_value(json.loads(bytes(raw)))
    if (
        canonicalize(binding) != bytes(raw)
        or hashlib.sha256(raw).digest() != message.notice_authorization_digest
    ):
        msg = "notice_authority_invalid"
        raise NoticeRefusalError(msg)
    return binding


def _verify_binding(
    message: OutboundMessage, scope: NoticeScope, binding: dict[str, JSONValue]
) -> None:
    if binding.get("schema") != SCHEMA:
        msg = "notice_authority_invalid"
        raise NoticeRefusalError(msg)
    if (
        integer_value(binding.get("issuer_membership_version"))
        != scope.issuer_membership_version
    ):
        msg = "issuer_not_allowed"
        raise NoticeRefusalError(msg)
    root_intent = text_value(binding.get("root_intent"))
    if root_intent != retry_root_intent(message):
        msg = "notice_authority_invalid"
        raise NoticeRefusalError(msg)
    if canonicalize(_envelope(scope, message, root_intent=root_intent)) != (
        message.notice_authorization_bytes
    ):
        msg = "notice_authority_invalid"
        raise NoticeRefusalError(msg)


def validate_notice(
    message: OutboundMessage, *, extra_actor: User | None = None
) -> tuple[bool, str]:
    """Rebuild the selected authority under live locks; never reselect consent."""
    try:
        binding = _read_binding(message)
        # PLATFORM_QUERY_OK: bound issuer is included in the complete lock union.
        actor = User.objects.get(pk=message.created_by_id)
        scope = lock_notice_scope(
            actor,
            NoticeOrigin(
                message.kind,
                message.client_id,
                message.cycle_id,
                message.evidence_request_id,
            ),
            binding=binding,
            extra_actor=extra_actor,
        )
        if binding is None:
            if not _legacy_publication(message, scope):
                return False, "notice_policy_missing"
            result = "legacy_published"
        else:
            _verify_binding(message, scope, binding)
            result = "authorized"
    except NoticeRefusalError as exc:
        return False, exc.reason
    except (PermissionDenied, Http404, User.DoesNotExist):
        return False, "issuer_not_allowed"
    except (ValidationError, ValueError, TypeError):
        return False, "notice_authority_invalid"
    else:
        return True, result
