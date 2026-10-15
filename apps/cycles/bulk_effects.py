"""Frozen per-kind inputs and database-only effects composed by the fenced runner."""

import json
from datetime import date
from typing import assert_never
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.accounts.models import User
from apps.authz.approvals import APPROVE, lock_authorities
from apps.authz.services import can
from apps.clients.maintenance import load_client
from apps.clients.models import (
    ClientAssignment,
    ClientCompany,
    ClientStatus,
    Engagement,
    TaxRegime,
)
from apps.clients.services import profile_at
from apps.cycles.bulk import BULK_CAPABILITY, BulkKind
from apps.cycles.bulk_contracts import (
    Selection,
    SnapshotItem,
    integer_value,
    list_value,
    object_value,
    text_value,
)
from apps.cycles.capabilities import CYCLES_OPERATE
from apps.cycles.effects.manifest import JSONValue
from apps.cycles.errors import VersionConflict
from apps.cycles.models import (
    ActionProposal,
    CycleEvent,
    EvidenceRequest,
    PlaybookVersion,
    ProposalState,
    RequestState,
    ServiceCycle,
    VersionState,
)
from apps.cycles.services.approvals import approve
from apps.cycles.services.cycle import (
    OPEN_STATES,
    check_version,
    migrate_open_cycle,
    open_cycle,
    tenant_id,
)
from apps.cycles.services.requests import Recipient, recipients
from apps.cycles.targeting import AppliesTo
from apps.delivery.models import OutboundMessage
from apps.delivery.notice_authority import NoticeOrigin, lock_notice_scope
from apps.delivery.outbox import enqueue, portal_home_url
from apps.delivery.policy import KINDS, BusinessIntent, render_body
from apps.tenants.models import Tenant


def _definition(selection: Selection) -> PlaybookVersion:
    if selection.playbook_version_id is None:
        msg = "Selecione a versão publicada do playbook."
        raise ValidationError(msg)
    return get_object_or_404(PlaybookVersion, pk=selection.playbook_version_id)


def _context_reason(
    client: ClientCompany,
    engagement: Engagement,
    definition: PlaybookVersion,
    period: date,
) -> str:
    if client.status != ClientStatus.ACTIVE:
        return "client_inactive"
    if definition.state != VersionState.PUBLISHED:
        return "playbook_unpublished"
    if engagement.starts_on > period or (
        engagement.ends_on is not None and engagement.ends_on <= period
    ):
        return "engagement_outside_period"
    profile = profile_at(client, period)
    scope = AppliesTo.parse(definition.applies_to_json)
    if (scope.service_kinds and engagement.service_kind not in scope.service_kinds) or (
        scope.regimes
        and (profile.regime if profile else TaxRegime.UNKNOWN) not in scope.regimes
    ):
        return "playbook_outside_scope"
    return ""


def _item(
    subject: Engagement | ServiceCycle | ActionProposal,
    client: ClientCompany,
    reason: str,
    effect: dict[str, JSONValue],
) -> SnapshotItem:
    return SnapshotItem(
        subject.pk,
        client.pk,
        client.legal_name,
        subject.version,
        not reason,
        reason,
        effect,
    )


def _preview_open(actor: User, pk: UUID, selection: Selection) -> SnapshotItem:
    engagement = get_object_or_404(Engagement, pk=pk)
    client = load_client(actor, tenant_id(), engagement.client_id)
    definition = _definition(selection)
    period = selection.period
    if period is None:
        msg = "Informe a competência dos ciclos."
        raise ValidationError(msg)
    reason = _context_reason(client, engagement, definition, period)
    if ServiceCycle.objects.filter(
        client=client,
        engagement=engagement,
        playbook_id=definition.playbook_id,
        period_start=period,
    ).exists():
        reason = "cycle_exists"
    if not can(actor, CYCLES_OPERATE, tenant_id=tenant_id()):
        reason = "capability_denied"
    profile = profile_at(client, period)
    return _item(
        engagement,
        client,
        reason,
        {
            "period": period.isoformat(),
            "playbook_version_id": str(definition.pk),
            "definition_version": definition.version,
            "playbook_name": definition.playbook.name,
            "target_number": definition.number,
            "profile_id": str(profile.pk) if profile else None,
            "owner_id": (
                str(engagement.responsible_id) if engagement.responsible_id else None
            ),
            "client_version": client.version,
        },
    )


def _recipient_data(recipient: Recipient) -> dict[str, JSONValue]:
    return {
        "contact_id": str(recipient.contact.pk),
        "version": recipient.contact.version,
        "email": recipient.contact.email,
        "access": recipient.access,
        "invite_id": str(recipient.invite.pk) if recipient.invite else None,
    }


def _notice_copy(tenant: Tenant) -> dict[str, JSONValue]:
    definition = KINDS["request_notice"]
    message = OutboundMessage(
        kind="request_notice", portal_url=portal_home_url(tenant.slug)
    )
    return {"subject": definition.subject, "body": render_body(message)}


def _preview_notices(actor: User, pk: UUID) -> SnapshotItem:
    cycle = get_object_or_404(ServiceCycle, pk=pk)
    client = load_client(actor, tenant_id(), cycle.client_id)
    drafts = list(cycle.requests.filter(state=RequestState.DRAFT).order_by("pk"))
    audience = recipients(client)
    eligible = [row for row in audience if row.eligible]
    reason = ""
    if cycle.state not in OPEN_STATES or client.status != ClientStatus.ACTIVE:
        reason = "cycle_closed"
    elif not drafts:
        reason = "no_drafts"
    elif not eligible:
        reason = "no_recipient"
    if not can(actor, CYCLES_OPERATE, tenant_id=tenant_id()):
        reason = "capability_denied"
    return _item(
        cycle,
        client,
        reason,
        {
            "period": cycle.period_start.isoformat(),
            "requests": [
                {
                    "id": str(row.pk),
                    "version": row.version,
                    "title": row.title,
                    "client_note": row.client_note,
                }
                for row in drafts
            ],
            "recipients": [_recipient_data(row) for row in eligible],
            "ineligible_recipients": [
                _recipient_data(row) for row in audience if not row.eligible
            ],
            **_notice_copy(client.tenant),
        },
    )


def _preview_migration(actor: User, pk: UUID, selection: Selection) -> SnapshotItem:
    cycle = get_object_or_404(ServiceCycle, pk=pk)
    client = load_client(actor, tenant_id(), cycle.client_id)
    target = _definition(selection)
    reason = _context_reason(client, cycle.engagement, target, cycle.period_start)
    if cycle.state not in OPEN_STATES:
        reason = "cycle_closed"
    elif target.playbook_id != cycle.playbook_id:
        reason = "different_playbook"
    elif target.number <= cycle.playbook_version.number:
        reason = "not_newer_version"
    if not can(actor, CYCLES_OPERATE, tenant_id=tenant_id()):
        reason = "capability_denied"
    return _item(
        cycle,
        client,
        reason,
        {
            "period": cycle.period_start.isoformat(),
            "playbook_version_id": str(target.pk),
            "definition_version": target.version,
            "playbook_name": target.playbook.name,
            "previous_number": cycle.playbook_version.number,
            "target_number": target.number,
            "steps": [
                {"key": step.key, "label": step.label_ptbr}
                for step in target.steps.filter(is_enabled=True).order_by("position")
            ],
        },
    )


def _preview_approval(actor: User, pk: UUID) -> SnapshotItem:
    proposal = get_object_or_404(ActionProposal, pk=pk)
    client = load_client(actor, tenant_id(), proposal.client_id)
    reason = ""
    if proposal.state != ProposalState.AWAITING:
        reason = "proposal_not_awaiting"
    elif proposal.expires_at <= timezone.now():
        reason = "proposal_expired"
    elif proposal.initiator_id == actor.pk:
        reason = "individual_self_approval"
    if not can(actor, APPROVE, tenant_id=tenant_id()):
        reason = "capability_denied"
    frozen: dict[str, JSONValue] = json.loads(bytes(proposal.manifest_bytes))
    return _item(
        proposal,
        client,
        reason,
        {
            "proposal_digest": proposal.digest,
            "frozen": frozen,
            "action_kind": proposal.action_kind,
            "action_label": proposal.get_action_kind_display(),
            "period": proposal.cycle.period_start.isoformat(),
        },
    )


def preview_item(
    actor: User, kind: BulkKind, pk: UUID, selection: Selection
) -> SnapshotItem:
    """Resolve one explicit, portfolio-scoped subject without changing it."""
    match kind:
        case BulkKind.OPEN_CYCLES:
            return _preview_open(actor, pk, selection)
        case BulkKind.SEND_REQUEST_NOTICES:
            return _preview_notices(actor, pk)
        case BulkKind.MIGRATE_OPEN_CYCLES:
            return _preview_migration(actor, pk, selection)
        case BulkKind.APPROVE_ITEMS:
            return _preview_approval(actor, pk)
    assert_never(kind)


def _lock_context(actor: User, client_id: UUID) -> tuple[User, ClientCompany]:
    authorities = lock_authorities({actor.pk}, tenant_id=tenant_id())
    authority = authorities[actor.pk]
    if (
        authority.membership is None
        or not authority.user.is_active
        or authority.user.is_superuser
    ):
        raise PermissionDenied
    # PLATFORM_QUERY_OK: the exact task tenant, after its actor's authority fence.
    Tenant.objects.select_for_update(no_key=True).get(pk=tenant_id())
    client = load_client(authority.user, tenant_id(), client_id, lock=True)
    list(
        ClientAssignment.objects.select_for_update(no_key=True)
        .filter(client=client, user=authority.user)
        .order_by("pk")
    )
    return authority.user, client


def _require_current(actor: User, client_id: UUID, capability: str) -> None:
    load_client(actor, tenant_id(), client_id)
    if not (
        actor.is_active
        and not actor.is_superuser
        and can(actor, BULK_CAPABILITY, tenant_id=tenant_id())
        and can(actor, capability, tenant_id=tenant_id())
    ):
        raise PermissionDenied


def _changed() -> VersionConflict:
    return VersionConflict("Os dados conferidos mudaram. Gere outra prévia.")


def _execute_open(
    actor: User, item: SnapshotItem, effect_key: str
) -> dict[str, JSONValue]:
    fresh, client = _lock_context(actor, item.client_id)
    engagement = get_object_or_404(
        Engagement.objects.select_for_update(),
        pk=item.subject_ref,
        client_id=client.pk,
    )
    check_version(engagement.version, item.expected_version)
    effect = item.effect
    definition = get_object_or_404(
        PlaybookVersion.objects.select_for_update(),
        pk=UUID(text_value(effect["playbook_version_id"])),
    )
    period = date.fromisoformat(text_value(effect["period"]))
    profile = profile_at(client, period)
    if (
        definition.version != integer_value(effect["definition_version"])
        or client.version != integer_value(effect["client_version"])
        or (str(profile.pk) if profile else None) != effect["profile_id"]
        or (str(engagement.responsible_id) if engagement.responsible_id else None)
        != effect["owner_id"]
        or _context_reason(client, engagement, definition, period)
    ):
        raise _changed()
    _require_current(fresh, client.pk, CYCLES_OPERATE)
    result = open_cycle(
        fresh, engagement.pk, period, definition.pk, idempotency_key=effect_key
    )
    return {
        "code": "cycle_opened" if result.created else "cycle_existing",
        "cycle_id": str(result.id),
    }


def _execute_notices(
    actor: User, item: SnapshotItem, effect_key: str
) -> dict[str, JSONValue]:
    candidate = get_object_or_404(
        ServiceCycle.objects, pk=item.subject_ref, client_id=item.client_id
    )
    scope = lock_notice_scope(
        actor,
        NoticeOrigin("request_notice", item.client_id, candidate.pk),
    )
    fresh, client = _lock_context(scope.actor, item.client_id)
    cycle = get_object_or_404(
        ServiceCycle.objects.select_for_update(), pk=item.subject_ref, client=client
    )
    check_version(cycle.version, item.expected_version)
    if cycle.state not in OPEN_STATES or client.status != ClientStatus.ACTIVE:
        raise _changed()
    expected = {
        UUID(text_value(row["id"])): row
        for raw in list_value(item.effect["requests"])
        for row in [object_value(raw)]
    }
    drafts = list(
        EvidenceRequest.objects.select_for_update()
        .filter(cycle=cycle, pk__in=expected)
        .order_by("pk")
    )
    if len(drafts) != len(expected) or any(
        row.state != RequestState.DRAFT
        or row.version != integer_value(expected[row.pk]["version"])
        for row in drafts
    ):
        raise _changed()
    # Compare only the frozen audience. A new contact can never join this batch.
    current = {str(row.contact.pk): row for row in recipients(client)}
    audience = []
    for raw in list_value(item.effect["recipients"]):
        selected = object_value(raw)
        recipient = current.get(text_value(selected["contact_id"]))
        if recipient is None or _recipient_data(recipient) != selected:
            raise _changed()
        audience.append(recipient)
    copy = _notice_copy(client.tenant)
    if any(copy[field] != item.effect[field] for field in ("subject", "body")):
        raise _changed()
    _require_current(fresh, client.pk, CYCLES_OPERATE)
    now = timezone.now()
    for request in drafts:
        request.state = RequestState.REQUESTED
        request.requested_at = now
        request.version += 1
        request.save(update_fields=["state", "requested_at", "version"])
        CycleEvent.objects.create(
            tenant_id=cycle.tenant_id,
            client=client,
            cycle=cycle,
            actor=fresh,
            type="request.sent",
            aggregate_type="EvidenceRequest",
            aggregate_id=request.pk,
            aggregate_version=request.version,
            payload={"from": RequestState.DRAFT, "to": RequestState.REQUESTED},
        )
    cycle.version += 1
    cycle.save(update_fields=["version"])
    ids: list[JSONValue] = []
    for recipient in audience:
        client.refresh_from_db(fields=["version"])
        message = enqueue(
            fresh,
            client.pk,
            recipient.contact.pk,
            intent=BusinessIntent(
                cycle.engagement_id,
                cycle.playbook_id,
                cycle.period_start,
                cycle.period_end,
                cycle.amendment,
                f"request_notice:{cycle.pk}:{recipient.contact.pk}",
                "request_notice",
                cycle.version,
            ),
            expected_version=client.version,
            kind="request_notice",
            cycle_id=cycle.pk,
            idempotency_key=f"{effect_key}:{recipient.contact.pk}",
            invite=recipient.invite,
            notice_scope=scope,
        )
        ids.append(str(message.pk))
    return {"code": "notices_queued", "message_ids": ids}


def _execute_migration(actor: User, item: SnapshotItem) -> dict[str, JSONValue]:
    fresh, client = _lock_context(actor, item.client_id)
    cycle = get_object_or_404(
        ServiceCycle.objects.select_for_update(), pk=item.subject_ref, client=client
    )
    check_version(cycle.version, item.expected_version)
    definition = get_object_or_404(
        PlaybookVersion.objects.select_for_update(),
        pk=UUID(text_value(item.effect["playbook_version_id"])),
    )
    if definition.version != integer_value(item.effect["definition_version"]) or (
        _context_reason(client, cycle.engagement, definition, cycle.period_start)
    ):
        raise _changed()
    _require_current(fresh, client.pk, CYCLES_OPERATE)
    result = migrate_open_cycle(
        fresh, cycle.pk, definition.pk, expected_version=item.expected_version
    )
    return {"code": "cycle_migrated", "cycle_id": str(result.id)}


def _execute_approval(actor: User, item: SnapshotItem) -> dict[str, JSONValue]:
    # Do not lock this row before T15's authority -> client -> cycle fences.
    candidate = get_object_or_404(
        ActionProposal, pk=item.subject_ref, client_id=item.client_id
    )
    if candidate.digest != text_value(item.effect["proposal_digest"]):
        raise _changed()
    result = approve(actor, candidate.pk, expected_version=item.expected_version)
    _require_current(actor, item.client_id, APPROVE)
    if result.proposal.state == ProposalState.INVALIDATED:
        # T15 deliberately returns invalidation so its reasons commit. Do not
        # turn that result into an exception that undoes the historical finding.
        return {"code": "proposal_invalidated", "proposal_id": str(candidate.pk)}
    return {
        "code": "approval_recorded",
        "proposal_id": str(candidate.pk),
        "digest": candidate.digest,
    }


@transaction.atomic
def execute_effect(
    actor: User, kind: BulkKind, item: SnapshotItem, effect_key: str
) -> dict[str, JSONValue]:
    """One savepoint: either the exact database effect and result commit or neither."""
    match kind:
        case BulkKind.OPEN_CYCLES:
            result = _execute_open(actor, item, effect_key)
        case BulkKind.SEND_REQUEST_NOTICES:
            result = _execute_notices(actor, item, effect_key)
        case BulkKind.MIGRATE_OPEN_CYCLES:
            result = _execute_migration(actor, item)
        case BulkKind.APPROVE_ITEMS:
            result = _execute_approval(actor, item)
        case _:
            assert_never(kind)
    # All source/authority locks remain held. A grant expiring during work rolls
    # back the effect, rather than committing it on the earlier can() answer.
    _require_current(
        actor,
        item.client_id,
        APPROVE if kind == BulkKind.APPROVE_ITEMS else CYCLES_OPERATE,
    )
    return result
