"""Decide on exact frozen effects; no executor, provider call or external write."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditAction
from apps.audit.services import ObjectRef, record_event
from apps.authz.approvals import (
    APPROVE,
    Authority,
    active_staff_count,
    is_policy_owner,
    lock_authorities,
)
from apps.clients.maintenance import load_client
from apps.clients.models import (
    ClientAssignment,
    ClientCompany,
    ClientContact,
    ClientStatus,
)
from apps.cycles.effects.build import (
    BuildError,
    ManifestContext,
    build_manifest,
    initiation_capability,
    lock_sources,
)
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.effects.invalidation import (
    Impact,
    InvalidationReason,
    RevalidationContext,
    compare_manifests,
)
from apps.cycles.effects.manifest import ActionKind, EffectManifestV1
from apps.cycles.errors import IllegalTransition, VersionConflict
from apps.cycles.models import (
    ActionExecution,
    ActionProposal,
    Approval,
    ApprovalDecision,
    CycleEvent,
    ProposalState,
    ServiceCycle,
)
from apps.cycles.services.cycle import (
    OPEN_STATES,
    check_version,
    require_enabled,
    tenant_id,
)
from apps.cycles.services.exceptions import open_exception
from apps.delivery.models import OutboundMessage
from apps.delivery.policy import BusinessIntent
from apps.tenants.models import Tenant

EXCEPTION_REASON = "approval_invalidated"
TERMINAL_STATES = frozenset(
    {
        ProposalState.REJECTED,
        ProposalState.INVALIDATED,
        ProposalState.CANCELLED,
    }
)


@dataclass(frozen=True, slots=True)
class LockedScope:
    """The common authority -> policy -> client -> cycle transaction fence."""

    authorities: dict[UUID, Authority]
    tenant: Tenant
    client: ClientCompany
    cycle: ServiceCycle
    actor_id: UUID
    system: bool = False

    @property
    def actor(self) -> User:
        """Return the actual user row read under the activity fence."""
        return self.authorities[self.actor_id].user

    def require(self, capability: str) -> None:
        """Keep the matrix and portfolio check inside every command."""
        if not self.authorities[self.actor_id].permits(
            capability, self.client.pk, self.tenant.pk
        ):
            raise PermissionDenied


@dataclass(frozen=True, slots=True)
class DecisionResult:
    """Invalidation is returned after it persists, never rolled back by an error."""

    proposal: ActionProposal
    reasons: tuple[InvalidationReason, ...] = ()

    @property
    def executable(self) -> bool:
        """Approval and every live execution condition must hold together."""
        return self.proposal.state == ProposalState.APPROVED and not any(
            reason.blocks_execution for reason in self.reasons
        )


def _recipient_users(spec: ProposalInput) -> set[UUID]:
    if spec.action_kind is not ActionKind.CLIENT_PUBLICATION:
        return set()
    return set(
        ClientContact.objects.filter(
            pk__in=spec.recipient_ids, membership__isnull=False
        ).values_list("membership__user_id", flat=True)
    )


def _lock_scope(
    actor: User,
    candidate: ServiceCycle,
    participants: set[UUID],
    *,
    system: bool = False,
) -> LockedScope:
    authorities = lock_authorities(participants | {actor.pk}, tenant_id=tenant_id())
    fresh = authorities[actor.pk]
    if not system and (
        not fresh.user.is_active or fresh.user.is_superuser or fresh.membership is None
    ):
        raise PermissionDenied
    # PLATFORM_QUERY_OK: a single explicit current tenant, after ordinary firm
    # membership discovery; all capability decisions are repeated under this fence.
    tenant = Tenant.objects.select_for_update(no_key=True).get(pk=tenant_id())
    client = (
        ClientCompany.objects.select_for_update(no_key=True)
        .filter(pk=candidate.client_id)
        .first()
    )
    if client is None:
        raise Http404
    list(
        ClientAssignment.objects.select_for_update(no_key=True)
        .filter(client=client, user_id__in=participants | {actor.pk})
        .order_by("pk")
    )
    if not system:
        load_client(fresh.user, tenant.pk, client.pk)
    cycle = ServiceCycle.objects.select_for_update(no_key=True).get(pk=candidate.pk)
    if cycle.client_id != client.pk:
        msg = "O contexto do ciclo mudou. Recarregue antes de decidir."
        raise VersionConflict(msg)
    return LockedScope(authorities, tenant, client, cycle, actor.pk, system)


def _cycle(cycle_id: UUID) -> ServiceCycle:
    require_enabled()
    result = ServiceCycle.objects.filter(pk=cycle_id).first()
    if result is None:
        raise Http404
    return result


def _event(
    scope: LockedScope,
    proposal: ActionProposal,
    event_type: str,
    *,
    extra: dict[str, str | int] | None = None,
) -> None:
    CycleEvent.objects.create(
        tenant=scope.tenant,
        client=scope.client,
        cycle=scope.cycle,
        actor=None if scope.system else scope.actor,
        type=event_type,
        aggregate_type="ActionProposal",
        aggregate_id=proposal.pk,
        aggregate_version=proposal.version,
        payload={
            "proposal_revision": proposal.revision,
            "digest": proposal.digest,
            **(extra or {}),
        },
    )


def _locked_proposal(
    actor: User | None, proposal_id: UUID, expected_version: int | None
) -> tuple[LockedScope, ActionProposal, ProposalInput, tuple[Approval, ...]]:
    require_enabled()
    candidate = ActionProposal.objects.filter(pk=proposal_id).first()
    if candidate is None:
        raise Http404
    reader = actor if actor is not None else candidate.initiator
    spec = ProposalInput.from_data(candidate.input_spec)
    previous_ids = set(
        Approval.objects.filter(proposal=candidate).values_list("pk", flat=True)
    )
    participants = (
        {candidate.initiator_id, reader.pk}
        | set(
            Approval.objects.filter(pk__in=previous_ids).values_list(
                "approver_id", flat=True
            )
        )
        | _recipient_users(spec)
    )
    scope = _lock_scope(
        reader, _cycle(candidate.cycle_id), participants, system=actor is None
    )
    proposal = ActionProposal.objects.select_for_update(no_key=True).get(pk=proposal_id)
    if expected_version is not None:
        check_version(proposal.version, expected_version)
    decisions = tuple(Approval.objects.filter(proposal=proposal).order_by("pk"))
    if {row.pk for row in decisions} != previous_ids or not _recipient_users(
        spec
    ).issubset(scope.authorities):
        msg = "A autoridade da proposta mudou. Recarregue antes de decidir."
        raise VersionConflict(msg)
    return scope, proposal, spec, decisions


def _approval_reasons(
    scope: LockedScope, decisions: tuple[Approval, ...], *, now: datetime
) -> tuple[InvalidationReason, ...]:
    reasons: list[InvalidationReason] = []
    for decision in decisions:
        if decision.decision != ApprovalDecision.APPROVE:
            continue
        authority = scope.authorities[decision.approver_id]
        if decision.expires_at <= now:
            reasons.append(InvalidationReason("approval_expired", Impact.INVALIDATE))
        elif not authority.permits(APPROVE, scope.client.pk, scope.tenant.pk):
            reasons.append(
                InvalidationReason("approver_role_revoked", Impact.INVALIDATE)
            )
        elif decision.authorization_snapshot["authority"] != authority.snapshot_data(
            APPROVE, client_id=scope.client.pk, now=now
        ):
            reasons.append(
                InvalidationReason("approver_authority_changed", Impact.INVALIDATE)
            )
    return tuple(dict.fromkeys(reasons))


def _invalidate(
    scope: LockedScope,
    proposal: ActionProposal,
    reasons: tuple[InvalidationReason, ...],
    *,
    now: datetime,
) -> None:
    if proposal.state in TERMINAL_STATES:
        return
    proposal.state = ProposalState.INVALIDATED
    proposal.version += 1
    proposal.invalidation_reasons = [
        reason.code for reason in reasons if reason.invalidates
    ]
    proposal.save(update_fields=["state", "version", "invalidation_reasons"])
    _event(scope, proposal, "proposal.invalidated")
    # This trusted opener must survive a revoked initiator. Its owner is the
    # fresh authorized reader, not an unvalidated stale proposal actor.
    open_exception(
        EXCEPTION_REASON,
        proposal.pk,
        actor=None,
        cycle_id=scope.cycle.pk,
        expected_version=scope.cycle.version,
        owner=scope.cycle.owner or scope.actor,
        review_on=now.astimezone(ZoneInfo("America/Sao_Paulo")).date(),
    )
    scope.cycle.refresh_from_db()


def _manifest(
    scope: LockedScope, proposal: ActionProposal, spec: ProposalInput
) -> tuple[EffectManifestV1, datetime]:
    sources = lock_sources(scope.cycle, spec)
    now = timezone.now()
    initiator = scope.authorities[proposal.initiator_id]
    if not initiator.permits(
        initiation_capability(spec.action_kind), scope.client.pk, scope.tenant.pk
    ):
        code = "initiator_authority_revoked"
        raise BuildError(code)
    if (
        scope.cycle.state not in OPEN_STATES
        or scope.client.status != ClientStatus.ACTIVE
    ):
        code = "effect_context_closed"
        raise BuildError(code)
    if sources.engagement.starts_on > scope.cycle.period_start or (
        sources.engagement.ends_on is not None
        and sources.engagement.ends_on <= scope.cycle.period_start
    ):
        code = "engagement_changed"
        raise BuildError(code)
    return build_manifest(
        proposal,
        spec,
        ManifestContext(scope.cycle, scope.tenant, sources, scope.authorities, now),
    ), now


def _revalidate(
    scope: LockedScope,
    proposal: ActionProposal,
    spec: ProposalInput,
    decisions: tuple[Approval, ...],
    *,
    decision_capability: str | None = None,
) -> DecisionResult:
    if proposal.state in TERMINAL_STATES:
        return DecisionResult(
            proposal, (InvalidationReason("proposal_terminal", Impact.INVALIDATE),)
        )
    try:
        live, now = _manifest(scope, proposal, spec)
        reasons = compare_manifests(
            bytes(proposal.manifest_bytes),
            live.canonical_bytes(),
            context=RevalidationContext(
                now=now, approver_authorized=True, grant_access_valid=True
            ),
        ) + _approval_reasons(scope, decisions, now=now)
    except BuildError as error:
        now = timezone.now()
        reasons = (InvalidationReason(error.code, Impact.INVALIDATE),)
    if decision_capability is not None:
        # Source fences can block after the entry check. A snapshot describes
        # authority; it never grants authority that expired during that wait.
        scope.require(decision_capability)
    if any(reason.invalidates for reason in reasons):
        _invalidate(scope, proposal, reasons, now=now)
    return DecisionResult(proposal, reasons)


def _intent(cycle: ServiceCycle, spec: ProposalInput) -> str:
    return BusinessIntent(
        cycle.engagement_id,
        cycle.playbook_id,
        cycle.period_start,
        cycle.period_end + timedelta(days=1),
        cycle.amendment,
        spec.step_key,
        spec.action_kind,
        spec.occurrence,
    ).key(cycle.tenant_id)


def _unstarted(proposal: ActionProposal) -> None:
    """Read under proposal/authority fences, never invert message -> cycle locks."""
    internal_effect = (
        ActionExecution.objects.filter(proposal=proposal)
        .exclude(action_kind=ActionKind.EMAIL_NOTICE)
        .exists()
    )
    dispatch_started = OutboundMessage.objects.filter(
        proposal_id=proposal.pk, send_started_at__isnull=False
    ).exists()
    if internal_effect or dispatch_started:
        msg = "Este efeito já foi iniciado. Cancelar a proposta não desfaz a execução."
        raise IllegalTransition(msg)


@transaction.atomic
def propose(
    actor: User,
    cycle_id: UUID,
    draft: ProposalDraft,
    *,
    expected_version: int,
) -> ActionProposal:
    """Freeze selected effects; revision is a new row under one natural intent."""
    spec = draft.inputs
    expires_at, not_before = draft.expires_at, draft.not_before
    spec.validate()
    scope = _lock_scope(actor, _cycle(cycle_id), {actor.pk} | _recipient_users(spec))
    check_version(scope.cycle.version, expected_version)
    scope.require(initiation_capability(spec.action_kind))
    if (
        scope.cycle.state not in OPEN_STATES
        or scope.client.status != ClientStatus.ACTIVE
    ):
        msg = "Este contexto está encerrado."
        raise IllegalTransition(msg)
    key = _intent(scope.cycle, spec)
    previous = (
        ActionProposal.objects.select_for_update(no_key=True)
        .filter(cycle=scope.cycle, business_intent_key=key)
        .order_by("-revision")
        .first()
    )
    if previous is not None:
        if draft.previous_id != previous.pk or draft.previous_version is None:
            msg = "Revise a proposta existente antes de criar outra versão."
            raise VersionConflict(msg)
        check_version(previous.version, draft.previous_version)
        _unstarted(previous)
        if previous.initiator_id != actor.pk and not is_policy_owner(
            scope.authorities[actor.pk]
        ):
            raise PermissionDenied
    elif draft.previous_id is not None:
        raise Http404
    proposal = ActionProposal(
        tenant=scope.tenant,
        client=scope.client,
        cycle=scope.cycle,
        initiator=scope.actor,
        previous=previous,
        revision=previous.revision + 1 if previous is not None else 1,
        business_intent_key=key,
        action_kind=spec.action_kind,
        input_spec=spec.as_data(),
        expires_at=expires_at,
        not_before=not_before,
    )
    manifest, now = _manifest(scope, proposal, spec)
    if (
        timezone.is_naive(expires_at)
        or (not_before is not None and timezone.is_naive(not_before))
        or expires_at <= now
        or (not_before is not None and not_before >= expires_at)
    ):
        msg = "Informe uma validade futura e um início anterior ao vencimento."
        raise ValidationError(msg)
    if not _recipient_users(spec).issubset(scope.authorities):
        msg = "Os destinatários mudaram. Recarregue a proposta."
        raise VersionConflict(msg)
    proposal.manifest_bytes = manifest.canonical_bytes()
    proposal.digest = manifest.digest
    proposal.save(force_insert=True)
    if previous is not None and previous.state not in TERMINAL_STATES:
        previous.state = ProposalState.CANCELLED
        previous.version += 1
        previous.save(update_fields=["state", "version"])
        _event(scope, previous, "proposal.superseded")
    scope.cycle.version += 1
    scope.cycle.save(update_fields=["version"])
    _event(scope, proposal, "proposal.created")
    return proposal


def _awaiting(proposal: ActionProposal) -> None:
    if proposal.state != ProposalState.AWAITING:
        msg = "Esta proposta já recebeu uma decisão. Recarregue o cartão."
        raise IllegalTransition(msg)


@transaction.atomic
def approve(
    actor: User,
    proposal_id: UUID,
    *,
    expected_version: int,
    self_approval_acknowledged: bool = False,
) -> DecisionResult:
    """Append one independent decision, or persist why its exact effects changed."""
    scope, proposal, spec, decisions = _locked_proposal(
        actor, proposal_id, expected_version
    )
    scope.require(APPROVE)
    _awaiting(proposal)
    result = _revalidate(scope, proposal, spec, decisions, decision_capability=APPROVE)
    if proposal.state == ProposalState.INVALIDATED:
        return result
    self_approval = proposal.initiator_id == scope.actor_id
    if self_approval and not (
        scope.tenant.allow_self_approval
        and active_staff_count(scope.tenant.pk) == 1
        and self_approval_acknowledged
    ):
        raise PermissionDenied
    if any(row.approver_id == scope.actor_id for row in decisions):
        msg = "Sua decisão já está registrada nesta versão."
        raise IllegalTransition(msg)
    now = timezone.now()
    if proposal.expires_at <= now:
        reasons = (InvalidationReason("proposal_expired", Impact.INVALIDATE),)
        _invalidate(scope, proposal, reasons, now=now)
        return DecisionResult(proposal, reasons)
    authority = scope.authorities[scope.actor_id]
    member_snapshot = authority.snapshot(APPROVE, client_id=scope.client.pk, now=now)
    snapshot = authority.snapshot_data(APPROVE, client_id=scope.client.pk, now=now)
    grant_expiries = [
        row.expires_at
        for row in authority.user_grants
        if row.revoked_at is None
        and row.expires_at is not None
        and row.capability.slug == APPROVE
    ]
    approval_expires_at = min([proposal.expires_at, *grant_expiries])
    if approval_expires_at <= now:
        raise PermissionDenied
    decision = Approval.objects.create(
        tenant=scope.tenant,
        client=scope.client,
        proposal=proposal,
        proposal_revision=proposal.revision,
        digest=proposal.digest,
        approver=scope.actor,
        membership_id=member_snapshot["membership_id"],
        membership_version=member_snapshot["membership_version"],
        authorization_snapshot={
            "authority": snapshot,
            "self_approval_acknowledged": self_approval and self_approval_acknowledged,
            "roster_version": scope.tenant.approval_roster_version,
        },
        decision=ApprovalDecision.APPROVE,
        decided_at=now,
        expires_at=approval_expires_at,
    )
    proposal.version += 1
    if len(decisions) + 1 >= scope.tenant.approval_quorum:
        proposal.state = ProposalState.APPROVED
        proposal.approved_at = now
    proposal.save(update_fields=["version", "state", "approved_at"])
    _event(
        scope,
        proposal,
        "proposal.approved",
        extra={"decision_id": str(decision.pk), "self_approval": int(self_approval)},
    )
    return DecisionResult(proposal, result.reasons)


@transaction.atomic
def reject(
    actor: User, proposal_id: UUID, *, expected_version: int, reason: str
) -> DecisionResult:
    """Preserve a rejection and its protected reason without changing frozen bytes."""
    scope, proposal, _spec, _decisions = _locked_proposal(
        actor, proposal_id, expected_version
    )
    scope.require(APPROVE)
    _awaiting(proposal)
    if not reason.strip():
        msg = "Informe o motivo para não aprovar."
        raise ValidationError(msg)
    now = timezone.now()
    authority = scope.authorities[actor.pk]
    member_snapshot = authority.snapshot(APPROVE, client_id=scope.client.pk, now=now)
    snapshot = authority.snapshot_data(APPROVE, client_id=scope.client.pk, now=now)
    Approval.objects.create(
        tenant=scope.tenant,
        client=scope.client,
        proposal=proposal,
        proposal_revision=proposal.revision,
        digest=proposal.digest,
        approver=scope.actor,
        membership_id=member_snapshot["membership_id"],
        membership_version=member_snapshot["membership_version"],
        authorization_snapshot={"authority": snapshot},
        decision=ApprovalDecision.REJECT,
        decided_at=now,
        expires_at=proposal.expires_at,
    )
    proposal.state = ProposalState.REJECTED
    proposal.decision_reason = reason.strip()
    proposal.version += 1
    proposal.save(update_fields=["state", "decision_reason", "version"])
    _event(scope, proposal, "proposal.rejected")
    return DecisionResult(proposal)


@transaction.atomic
def cancel(actor: User, proposal_id: UUID, *, expected_version: int) -> DecisionResult:
    """Withdraw an unexecuted proposal; append-only approvals remain historical."""
    scope, proposal, spec, _decisions = _locked_proposal(
        actor, proposal_id, expected_version
    )
    scope.require(initiation_capability(spec.action_kind))
    if proposal.initiator_id != actor.pk and not is_policy_owner(
        scope.authorities[actor.pk]
    ):
        raise PermissionDenied
    if proposal.state in TERMINAL_STATES:
        msg = "Esta proposta já está encerrada."
        raise IllegalTransition(msg)
    _unstarted(proposal)
    proposal.state = ProposalState.CANCELLED
    proposal.version += 1
    proposal.save(update_fields=["state", "version"])
    _event(scope, proposal, "proposal.cancelled")
    return DecisionResult(proposal)


@transaction.atomic
def revalidate(
    actor: User | None, proposal_id: UUID, *, expected_version: int | None = None
) -> DecisionResult:
    """Rebuild under locks; None is the trusted dispatcher's non-impersonating read."""
    scope, proposal, spec, decisions = _locked_proposal(
        actor, proposal_id, expected_version
    )
    authority = scope.authorities[scope.actor_id]
    if actor is not None and not (
        authority.permits(APPROVE, scope.client.pk, scope.tenant.pk)
        or authority.permits(
            initiation_capability(spec.action_kind), scope.client.pk, scope.tenant.pk
        )
    ):
        raise PermissionDenied
    result = _revalidate(scope, proposal, spec, decisions)
    if result.executable:
        approved = [
            row for row in decisions if row.decision == ApprovalDecision.APPROVE
        ]
        separated = not (
            scope.tenant.allow_self_approval
            and active_staff_count(scope.tenant.pk) == 1
        )
        if len(
            {row.approver_id for row in approved}
        ) < scope.tenant.approval_quorum or (
            separated
            and any(row.approver_id == proposal.initiator_id for row in approved)
        ):
            reasons = (
                InvalidationReason("approval_quorum_missing", Impact.INVALIDATE),
            )
            _invalidate(scope, proposal, reasons, now=timezone.now())
            return DecisionResult(proposal, reasons)
    return result


@transaction.atomic
def configure_approval_policy(
    actor: User,
    *,
    expected_version: int,
    allow_self_approval: bool,
    acknowledged: bool,
    quorum: int = 1,
) -> Tenant:
    """Record an owner-only exception whose live use ends when staff is added."""
    require_enabled()
    authorities = lock_authorities({actor.pk}, tenant_id=tenant_id())
    # PLATFORM_QUERY_OK: one explicit tenant policy, authorized by its fresh owner.
    tenant = Tenant.objects.select_for_update(no_key=True).get(pk=tenant_id())
    authority = authorities[actor.pk]
    if not is_policy_owner(authority):
        raise PermissionDenied
    check_version(tenant.approval_policy_version, expected_version)
    count = active_staff_count(tenant.pk)
    if quorum < 1 or (allow_self_approval and (not acknowledged or count != 1)):
        msg = "A autoaprovação exige um único integrante ativo e confirmação explícita."
        raise ValidationError(msg)
    if (
        tenant.allow_self_approval == allow_self_approval
        and tenant.approval_quorum == quorum
    ):
        return tenant
    tenant.allow_self_approval = allow_self_approval
    tenant.approval_quorum = quorum
    tenant.save(update_fields=["allow_self_approval", "approval_quorum"])
    tenant.refresh_from_db()
    metadata: dict[str, object] = {
        "change": "approval_policy",
        "allow_self_approval": allow_self_approval,
        "acknowledged": acknowledged,
        "quorum": quorum,
        "policy_version": tenant.approval_policy_version,
        "roster_version": tenant.approval_roster_version,
        "active_staff": count,
    }
    record_event(
        action=AuditAction.TENANT_UPDATED,
        tenant_id=tenant.pk,
        actor=authority.user,
        obj=ObjectRef(type="approval_policy", id=str(tenant.pk)),
        metadata=metadata,
    )
    return tenant
