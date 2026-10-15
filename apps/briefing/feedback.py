"""Record personal dismissals only against the fact version shown to the person."""

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from typing import Final
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404

from apps.accounts.models import User
from apps.audit.models import AuditAction
from apps.audit.services import ObjectRef, record_event
from apps.authz.portfolio import visible_clients
from apps.briefing import queries
from apps.briefing.coverage_source import coverage_items
from apps.briefing.providers import source_item
from apps.briefing.source_rows import SourceContext
from apps.clients.maintenance import load_client
from apps.clients.models import ClientCompany, Engagement
from apps.core.flags import tenant_has
from apps.cycles.errors import VersionConflict
from apps.cycles.models import (
    ActionProposal,
    BriefingFeedback,
    DocumentIntake,
    EvidenceRequest,
)
from apps.cycles.models import Exception as CycleException
from apps.cycles.services.cycle import require_actor, tenant_id
from apps.delivery.models import OutboundMessage
from apps.obligations.coverage import coverage_for
from apps.obligations.models import Obligation

type Subject = (
    Obligation
    | EvidenceRequest
    | CycleException
    | ActionProposal
    | OutboundMessage
    | DocumentIntake
    | Engagement
)

SUBJECT_MODELS: Final[dict[str, type[Subject]]] = {
    "obligation": Obligation,
    "request": EvidenceRequest,
    "exception": CycleException,
    "approval": ActionProposal,
    "delivery": OutboundMessage,
    "document": DocumentIntake,
    "coverage": Engagement,
}
REASON_LIMIT: Final = 500


@dataclass(frozen=True, slots=True)
class Dismissal:
    """The rendered identity, its version, and the person's stated reason."""

    kind: str
    subject_ref: UUID
    fact_version: str
    reason: str
    period: date | None = None


def dismiss(actor: User, command: Dismissal) -> BriefingFeedback:
    """Lock, re-authorize and compare the fact before recording any hidden item."""
    reason = command.reason.strip()
    if command.kind not in SUBJECT_MODELS or not reason or len(reason) > REASON_LIMIT:
        message = "Escolha um item e informe o motivo (até 500 caracteres)."
        raise ValidationError(message)
    if command.kind == "coverage" and command.period is None:
        message = "Informe a competência da cobertura."
        raise ValidationError(message)
    if not tenant_has(tenant_id(), "briefing_r1"):
        raise Http404
    moment = queries.clock()
    with transaction.atomic():
        model = SUBJECT_MODELS[command.kind]
        visible = visible_clients(actor, tenant_id=tenant_id())
        client_id: UUID | None = (
            model.objects.filter(
                pk=command.subject_ref, client_id__in=visible.values("pk")
            )
            .select_for_update()
            .values_list("client_id", flat=True)
            .first()
        )
        if client_id is None:
            raise Http404
        client = load_client(actor, tenant_id(), client_id)
        if command.kind == "coverage" and command.period is not None:
            # T17 serializes source changes on the obligation. Re-read its public
            # answer after acquiring that same lock; never classify freshness here.
            answers = coverage_for(
                actor,
                client,
                command.period,
                engagement_id=command.subject_ref,
                now=moment,
            )
            if answers and answers[0].obligation_id is not None:
                Obligation.objects.select_for_update().get(
                    pk=answers[0].obligation_id, client_id=client_id
                )
        require_actor(actor, "clients.view_assigned")
        client = load_client(actor, tenant_id(), client_id)
        scoped = ClientCompany.objects.filter(pk=client_id)
        if command.kind == "coverage" and command.period is not None:
            current = next(
                (
                    item
                    for item in coverage_items(
                        actor, [client], command.period, now=moment
                    )
                    if item.subject_ref == command.subject_ref
                ),
                None,
            )
        else:
            current = source_item(
                SourceContext(scoped, moment), command.kind, command.subject_ref
            )
        if current is None or current.fact_version != command.fact_version:
            message = "Este fato mudou. Recarregue a fila antes de decidir."
            raise VersionConflict(message)
        previous = BriefingFeedback.objects.filter(
            user=actor,
            client_id=client_id,
            kind=command.kind,
            subject_ref=command.subject_ref,
            fact_version=command.fact_version,
        )
        if previous.exists():
            message = "Esta versão do fato já foi marcada como não relevante."
            raise VersionConflict(message)
        feedback = BriefingFeedback.objects.create(
            tenant_id=tenant_id(),
            client_id=client_id,
            user=actor,
            kind=command.kind,
            subject_ref=command.subject_ref,
            fact_version=command.fact_version,
            reason=reason,
        )
        record_event(
            action=AuditAction.BRIEFING_DISMISSED,
            actor=actor,
            obj=ObjectRef("BriefingFeedback", str(feedback.pk)),
            metadata={
                "kind": command.kind,
                "subject_id": str(command.subject_ref),
                "fact_hash": sha256(command.fact_version.encode()).hexdigest(),
            },
        )
        return feedback
