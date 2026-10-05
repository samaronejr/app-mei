"""Load selected live facts under their real writers' transaction fences."""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage
from django.core.validators import validate_email
from django.db.models import Q

from apps.authz.approvals import Authority, active_staff_count
from apps.clients.models import ClientContact, Engagement, TaxProfile
from apps.cycles.effects.inputs import ProposalInput
from apps.cycles.effects.mail import freeze_mail, smtp_account
from apps.cycles.effects.manifest import (
    BOUND_COLLECTIONS,
    ActionKind,
    DocumentEffect,
    EffectManifestV1,
    EvidenceEffect,
    JSONValue,
    MonetaryEffect,
    ObservationEffect,
    Recipient,
    canonicalize,
    normalize_email,
)
from apps.cycles.evidence import PORTAL_SERVABLE_STATES, ReviewDecision
from apps.cycles.models import (
    ActionExecution,
    ActionProposal,
    ClientPublication,
    DocumentIntake,
    EvidenceLink,
    EvidenceRequest,
    ExtractedField,
    PlaybookVersion,
    PublicationState,
    Review,
    ServiceCycle,
)
from apps.obligations.models import Obligation, SourceObservation
from apps.tenants.models import Tenant


class BuildError(ValueError):
    """A selected source no longer qualifies; never replace it silently."""

    def __init__(self, code: str) -> None:
        """Keep one machine code for invalidation and the UI's translated label."""
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class LiveSources:
    """The selected cut of mutable sources, held until the decision commits."""

    contacts: tuple[ClientContact, ...]
    links: tuple[EvidenceLink, ...]
    requests: tuple[EvidenceRequest, ...]
    observations: tuple[SourceObservation, ...]
    profiles: tuple[TaxProfile, ...]
    engagement: Engagement
    definition: PlaybookVersion


@dataclass(frozen=True, slots=True)
class ManifestContext:
    """A live transaction's locked facts and final authority cut-point."""

    cycle: ServiceCycle
    tenant: Tenant
    sources: LiveSources
    authorities: dict[UUID, Authority]
    now: datetime


def initiation_capability(kind: ActionKind) -> str:
    """Completion has its own capability; the other proposals are cycle work."""
    return (
        "cycles.complete" if kind is ActionKind.CYCLE_COMPLETION else "cycles.operate"
    )


def lock_sources(cycle: ServiceCycle, spec: ProposalInput) -> LiveSources:
    """Run after client/cycle fences and before the final decision clock.

    Scan/extraction writers take intake then cycle. Selected intake/document
    projections are therefore READ under the cycle fence, not locked in reverse.
    Source observations instead fence their mutable subjects; their INSERT trigger
    takes the same fence. Append-only rows never receive UPDATE privileges.
    """
    bound = BOUND_COLLECTIONS[spec.action_kind]
    contact_ids = set(spec.recipient_ids) if "recipients" in bound else set()
    contacts = tuple(
        ClientContact.objects.select_for_update(no_key=True)
        .filter(client_id=cycle.client_id, pk__in=contact_ids)
        .order_by("pk")
    )
    if len(contacts) != len(contact_ids):
        code = "recipient_unavailable"
        raise BuildError(code)
    if spec.withdraw_publication_id is not None:
        target = (
            ClientPublication.objects.select_for_update(no_key=True)
            .filter(
                pk=spec.withdraw_publication_id,
                client_id=cycle.client_id,
                cycle=cycle,
            )
            .first()
        )
        if target is None:
            code = "publication_unavailable"
            raise BuildError(code)
    engagement = Engagement.objects.select_for_update(no_key=True).get(
        pk=cycle.engagement_id, client_id=cycle.client_id
    )
    profiles = tuple(
        TaxProfile.objects.select_for_update(no_key=True)
        .filter(
            Q(valid_to__isnull=True) | Q(valid_to__gt=cycle.period_start),
            client_id=cycle.client_id,
            valid_from__lte=cycle.period_end,
            superseded_at__isnull=True,
        )
        .order_by("pk")
    )
    definition = PlaybookVersion.objects.select_for_update(no_key=True).get(
        pk=cycle.playbook_version_id
    )
    link_ids = set(spec.document_link_ids) if "documents" in bound else set()
    if "money" in bound:
        link_ids.update(item.link_id for item in spec.money)
    links_query = EvidenceLink.objects.filter(
        client_id=cycle.client_id, request__cycle=cycle, pk__in=link_ids
    )
    request_ids = set(spec.evidence_request_ids) if "evidence" in bound else set()
    requests_to_lock = request_ids | set(
        links_query.values_list("request_id", flat=True)
    )
    requests = tuple(
        EvidenceRequest.objects.select_for_update(no_key=True)
        .filter(cycle=cycle, pk__in=requests_to_lock)
        .order_by("pk")
    )
    if not request_ids.issubset({row.pk for row in requests}):
        code = "evidence_unavailable"
        raise BuildError(code)
    links = tuple(
        links_query.select_for_update(no_key=True, of=("self",))
        .select_related("document")
        .order_by("pk")
    )
    if len(links) != len(link_ids):
        code = "document_unavailable"
        raise BuildError(code)
    observation_ids = set(spec.observation_ids) if "observations" in bound else set()
    observations = tuple(
        SourceObservation.objects.filter(
            client_id=cycle.client_id, pk__in=observation_ids
        ).order_by("pk")
    )
    if len(observations) != len(observation_ids):
        code = "observation_unavailable"
        raise BuildError(code)
    if any(row.period != cycle.period_start for row in observations):
        code = "observation_period_mismatch"
        raise BuildError(code)
    list(
        Obligation.objects.select_for_update(no_key=True)
        .filter(
            client_id=cycle.client_id,
            pk__in={row.subject_ref_id for row in observations},
        )
        .order_by("pk")
    )
    return LiveSources(
        contacts, links, requests, observations, profiles, engagement, definition
    )


def _recipients(
    sources: LiveSources, kind: ActionKind, authorities: dict[UUID, Authority]
) -> tuple[Recipient, ...]:
    result: list[Recipient] = []
    for contact in sources.contacts:
        if not contact.active:
            code = "recipient_ineligible"
            raise BuildError(code)
        channel, address = "email", contact.email
        if kind is ActionKind.CLIENT_PUBLICATION:
            membership = contact.membership
            if (
                membership is None
                or not membership.is_active
                or membership.client_id != contact.client_id
                or membership.user_id not in authorities
                or not authorities[membership.user_id].user.is_active
            ):
                code = "recipient_ineligible"
                raise BuildError(code)
            channel, address = "portal", str(membership.user_id)
        if not contact.channel_email_allowed:
            code = "recipient_ineligible"
            raise BuildError(code)
        try:
            validate_email(contact.email)
        except ValidationError as error:
            code = "recipient_ineligible"
            raise BuildError(code) from error
        if "\r" in contact.email or "\n" in contact.email:
            code = "recipient_ineligible"
            raise BuildError(code)
        result.append(
            {
                "role": "to",
                "contact_id": str(contact.pk),
                "channel": channel,
                "address": address,
            }
        )
        if kind is ActionKind.CLIENT_PUBLICATION:
            result.append(
                {
                    "role": "notice",
                    "contact_id": str(contact.pk),
                    "channel": "email",
                    "address": contact.email,
                }
            )
    return tuple(result)


def _documents(sources: LiveSources) -> tuple[DocumentEffect, ...]:
    reviews = {
        row.link_id: row
        for row in Review.objects.filter(
            link_id__in={link.pk for link in sources.links}
        )
    }
    intakes = {
        row.document_id: row
        for row in DocumentIntake.objects.filter(
            document_id__in={link.document_id for link in sources.links}
        )
    }
    result: list[DocumentEffect] = []
    for link in sources.links:
        intake = intakes.get(link.document_id)
        review = reviews.get(link.pk)
        if (
            intake is None
            or intake.state not in PORTAL_SERVABLE_STATES
            or link.review_decision != ReviewDecision.ACCEPTED
            or review is None
            or review.decision != ReviewDecision.ACCEPTED
            or review.document_sha256 != link.document.sha256
        ):
            code = "document_not_reviewed"
            raise BuildError(code)
        document = link.document
        result.append(
            {
                "document_id": str(document.pk),
                "link_id": str(link.pk),
                "link_revision": link.version,
                "sha256": document.sha256,
                "size": document.byte_size,
                "intake_state": intake.state,
                "scan_id": f"{intake.pk}:{intake.version}",
                "review_state": link.review_decision,
            }
        )
    return tuple(result)


def _money(sources: LiveSources, spec: ProposalInput) -> tuple[MonetaryEffect, ...]:
    if "money" not in BOUND_COLLECTIONS[spec.action_kind]:
        return ()
    documents = {link.pk: link.document_id for link in sources.links}
    result: list[MonetaryEffect] = []
    for selected in spec.money:
        document_id = documents[selected.link_id]
        fields = list(
            ExtractedField.objects.filter(
                document_id=document_id, name=selected.field_name
            ).order_by("-created_at", "-pk")
        )
        field = next((row for row in fields if row.origin == "staff"), None)
        if field is None:
            field = fields[0] if fields else None
        if field is None:
            code = "amount_unavailable"
            raise BuildError(code)
        try:
            amount = Decimal(field.value)
        except InvalidOperation as error:
            code = "amount_invalid"
            raise BuildError(code) from error
        result.append(
            MonetaryEffect(
                name=f"{document_id}:{selected.field_name}",
                amount=amount,
                currency=selected.currency,
                scale=selected.scale,
                rounding_rule=selected.rounding_rule,
            )
        )
    return tuple(result)


def _evidence(sources: LiveSources, spec: ProposalInput) -> tuple[EvidenceEffect, ...]:
    selected = set(spec.evidence_request_ids)
    result: list[EvidenceEffect] = []
    for request in sources.requests:
        if request.pk not in selected:
            continue
        decision = (
            Review.objects.filter(link__request=request)
            .order_by("-reviewed_at", "-pk")
            .first()
        )
        result.append(
            {
                "request_id": str(request.pk),
                "revision": request.version,
                "decision_id": str(decision.pk) if decision is not None else None,
            }
        )
    return tuple(result)


def _observations(sources: LiveSources) -> tuple[ObservationEffect, ...]:
    result: list[ObservationEffect] = []
    for row in sources.observations:
        if SourceObservation.objects.filter(
            supersedes=row,
            subject_ref_id=row.subject_ref_id,
            kind=row.kind,
            period=row.period,
        ).exists():
            code = "observation_superseded"
            raise BuildError(code)
        result.append(
            {
                "id": str(row.pk),
                "source_kind": row.source_kind,
                "status": row.verification_status,
                "observed_at": row.observed_at,
                "valid_until": (
                    row.observed_at + timedelta(days=7)
                    if row.source_kind == "official_api"
                    else None
                ),
            }
        )
    return tuple(result)


def build_manifest(
    proposal: ActionProposal,
    spec: ProposalInput,
    context: ManifestContext,
) -> EffectManifestV1:
    """Build from the locked cut, keeping proposal identity, copy and times fixed."""
    cycle, tenant = context.cycle, context.tenant
    sources, authorities, now = context.sources, context.authorities, context.now
    sole = tenant.allow_self_approval and active_staff_count(tenant.pk) == 1
    policy: dict[str, JSONValue] = {
        "allow_self_approval": tenant.allow_self_approval,
        "quorum": tenant.approval_quorum,
        "separation_of_duties": not sole,
        "engagement_version": sources.engagement.version,
    }
    if sole:
        policy["roster_version"] = tenant.approval_roster_version
    steps: list[JSONValue] = [
        {
            "key": step.key,
            "position": step.position,
            "kind": step.kind,
            "label_ptbr": step.label_ptbr,
            "client_label_ptbr": step.client_label_ptbr,
            "depends_on": step.depends_on,
            "owner_rule": step.owner_rule,
            "due_rule_json": step.due_rule_json,
            "evidence_required": step.evidence_required,
            "client_visible": step.client_visible,
            "is_enabled": step.is_enabled,
        }
        for step in sources.definition.steps.order_by("key")
    ]
    definition: dict[str, JSONValue] = {
        "id": str(sources.definition.pk),
        "number": sources.definition.number,
        "applies_to": sources.definition.applies_to_json,
        "steps": steps,
    }
    payload: dict[str, JSONValue] = {
        "subject": spec.subject,
        "body": spec.body,
        "reason": spec.reason,
    }
    if spec.action_kind is ActionKind.CYCLE_COMPLETION:
        payload["completion_state"] = spec.completion_state
    recipients = _recipients(sources, spec.action_kind, authorities)
    if spec.action_kind is ActionKind.EMAIL_NOTICE:
        provider = "smtp"
        account_id = smtp_account()
        payload.update(
            freeze_mail(
                proposal,
                EmailMessage(
                    subject=spec.subject,
                    body=spec.body,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    to=sorted(normalize_email(row["address"]) for row in recipients),
                ),
            )
        )
    else:
        provider, account_id = "axorim", str(tenant.pk)
    if spec.action_kind is ActionKind.CLIENT_PUBLICATION:
        from apps.delivery.outbox import portal_home_url  # noqa: PLC0415

        target_id = spec.withdraw_publication_id or proposal.pk
        url = f"{portal_home_url(tenant.slug)}publicacoes/{target_id}/"
        notice_subject = (
            "Publicação retirada pelo escritório"
            if spec.withdraw_publication_id
            else "Nova publicação no portal do cliente"
        )
        notice_body = f"{notice_subject}.\nAcesse o portal: {url}"
        notice: dict[str, JSONValue] = {
            "subject": notice_subject,
            "body": notice_body,
            "url": url,
            "account_id": smtp_account(),
            **freeze_mail(
                proposal,
                EmailMessage(
                    subject=notice_subject,
                    body=notice_body,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    to=sorted(
                        normalize_email(row["address"])
                        for row in recipients
                        if row["channel"] == "email"
                    ),
                ),
            ),
        }
        payload["notice"] = notice
        if spec.withdraw_publication_id is not None:
            target = ClientPublication.objects.get(pk=spec.withdraw_publication_id)
            version = target.version
            if target.state == PublicationState.WITHDRAWN and (
                ActionExecution.objects.filter(
                    proposal=proposal, digest=proposal.digest
                ).exists()
            ):
                # This exact committed effect advanced the target once. Rebuild
                # its approved precondition while still rechecking every live
                # recipient, authority and source before the notice dispatch.
                version -= 1
            elif target.state != PublicationState.PUBLISHED:
                code = "publication_unavailable"
                raise BuildError(code)
            payload["withdrawal"] = {
                "publication_id": str(target.pk),
                "digest": target.publication_digest,
                "version": version,
            }
    return EffectManifestV1(
        tenant_id=str(proposal.tenant_id),
        client_id=str(proposal.client_id),
        engagement_id=str(cycle.engagement_id),
        cycle_id=str(cycle.pk),
        cycle_revision=cycle.effect_revision,
        amendment=cycle.amendment,
        period_start=cycle.period_start,
        period_end=cycle.period_end + timedelta(days=1),
        playbook_version_id=str(cycle.playbook_version_id),
        playbook_hash=hashlib.sha256(canonicalize(definition)).hexdigest(),
        proposal_id=str(proposal.pk),
        proposal_revision=proposal.revision,
        business_intent_key=proposal.business_intent_key,
        action_kind=spec.action_kind,
        adapter_version="r1.1",
        confirmation_contract_version="r1.1",
        initiator_id=str(proposal.initiator_id),
        destination={
            "provider": provider,
            "account_id": account_id,
            "operation": spec.action_kind,
        },
        payload=payload,
        recipients=recipients,
        money=_money(sources, spec),
        documents=_documents(sources),
        evidence=_evidence(sources, spec),
        observations=_observations(sources),
        tax_profile_versions=tuple(str(row.pk) for row in sources.profiles),
        policy={
            "id": str(tenant.pk),
            "version": tenant.approval_policy_version,
            "hash": hashlib.sha256(canonicalize(policy)).hexdigest(),
        },
        authorization=authorities[proposal.initiator_id].snapshot(
            initiation_capability(spec.action_kind), client_id=cycle.client_id, now=now
        ),
        approval_rule={
            "required_capability": "actions.approve",
            "quorum": tenant.approval_quorum,
            "separation_of_duties": not sole,
        },
        not_before=proposal.not_before,
        expires_at=proposal.expires_at,
    )
