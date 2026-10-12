"""One request-local, portfolio-scoped read cut for the firm cycle detail."""

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, TypedDict
from uuid import UUID

from django.contrib.postgres.expressions import ArraySubquery
from django.db.models import Exists, OuterRef, Q, Subquery
from django.db.models.functions import JSONObject
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.authz.models import GrantLevel
from apps.authz.portfolio import visible_clients
from apps.authz.services import granted_levels
from apps.clients.models import TaxProfile
from apps.clients.services import claim_at
from apps.cycles.capabilities import (
    CYCLES_COMPLETE,
    CYCLES_OPERATE,
    CYCLES_REOPEN,
    CYCLES_REVIEW,
)
from apps.cycles.display_completion import display_valid_waivers
from apps.cycles.display_index import display_source_digest
from apps.cycles.display_recipients import RecipientPreview, display_recipients
from apps.cycles.display_requests import display_requests
from apps.cycles.models import (
    ActionExecution,
    ActionProposal,
    CycleState,
    DocumentIntake,
    EvidenceIndex,
    EvidenceLink,
    EvidenceRequest,
    InternalNote,
    ServiceCycle,
)
from apps.cycles.models import Exception as CycleException
from apps.cycles.services.cycle import require_enabled, tenant_id
from apps.delivery.models import MessageState, OutboundMessage
from apps.obligations.coverage import FACET_KINDS, CoverageItem, _engagement_state
from apps.obligations.models import (
    Obligation,
    ObservationSubjectKind,
    SourceObservation,
)

if TYPE_CHECKING:
    from django.db.models import QuerySet
    from django_stubs_ext import WithAnnotations

    from apps.accounts.models import User
    from apps.cycles.display_requests import RequestFacts
    from apps.cycles.models import CycleTask, PlaybookStep


class MessageHead(TypedDict):
    """The identity and revision needed by the displayed closure checklist."""

    id: str
    version: int


class CycleFacts(TypedDict):
    """Small closure facts selected alongside the authorized cycle row."""

    display_index_digest: str | None
    display_messages: list[MessageHead]
    display_has_notes: bool
    display_has_proposals: bool
    display_has_exceptions: bool


class ProposalFacts(TypedDict):
    """Execution existence without a second proposal or receipt collection read."""

    display_executed: bool


class LinkFacts(TypedDict):
    """The intake state already used by the request surface."""

    intake_state: str | None


@dataclass(frozen=True, slots=True)
class CycleDetailReads:
    """Display data only; never pass this cut to a command or permission answerer."""

    cycle: ServiceCycle
    steps: "tuple[PlaybookStep, ...]"
    tasks: "tuple[CycleTask, ...]"
    notes: "tuple[InternalNote, ...]"
    proposals: "tuple[WithAnnotations[ActionProposal, ProposalFacts], ...]"
    requests: "tuple[WithAnnotations[EvidenceRequest, RequestFacts], ...]"
    links: "tuple[WithAnnotations[EvidenceLink, LinkFacts], ...]"
    exceptions: tuple[CycleException, ...]
    obligations: tuple[Obligation, ...]
    observations: tuple[SourceObservation, ...]
    coverage: tuple[CoverageItem, ...]
    recipients: tuple[RecipientPreview, ...]
    allowed: frozenset[str]
    valid_waivers: frozenset[UUID]
    unresolved_messages: tuple[MessageHead, ...]
    index_current: bool


def _cycle(
    actor: "User", cycle_id: UUID
) -> "WithAnnotations[ServiceCycle, CycleFacts]":
    require_enabled()
    tenant = tenant_id()
    clients = visible_clients(actor, tenant_id=tenant)
    indexes = EvidenceIndex.objects.filter(
        tenant_id=tenant,
        client_id=OuterRef("client_id"),
        cycle_id=OuterRef("pk"),
        amendment=OuterRef("amendment"),
    ).order_by("-created_at", "-pk")
    messages = (
        OutboundMessage.objects.filter(
            cycle_id=OuterRef("pk"),
            state__in=[
                MessageState.QUEUED,
                MessageState.SENDING,
                MessageState.TEMPORARY,
                MessageState.UNKNOWN,
            ],
        )
        .order_by("pk")
        .values(head=JSONObject(id="pk", version="version"))
    )
    rows: QuerySet[WithAnnotations[ServiceCycle, CycleFacts]] = (
        ServiceCycle.objects.filter(pk=cycle_id, client_id__in=clients.values("pk"))
        .select_related("client", "engagement", "playbook", "playbook_version")
        .annotate(
            display_index_digest=Subquery(indexes.values("source_digest")[:1]),
            display_messages=ArraySubquery(messages),
            display_has_notes=Exists(
                InternalNote.objects.filter(cycle_id=OuterRef("pk"))
            ),
            display_has_proposals=Exists(
                ActionProposal.objects.filter(cycle_id=OuterRef("pk"))
            ),
            display_has_exceptions=Exists(
                CycleException.objects.filter(cycle_id=OuterRef("pk"))
            ),
        )
    )
    return get_object_or_404(rows)


def _coverage(
    cycle: ServiceCycle,
    obligations: tuple[Obligation, ...],
    observations: tuple[SourceObservation, ...],
) -> tuple[CoverageItem, ...]:
    engagement = cycle.engagement
    if engagement.starts_on > cycle.period_start or (
        engagement.ends_on is not None and engagement.ends_on <= cycle.period_start
    ):
        return ()
    profile = claim_at(
        list(TaxProfile.objects.filter(client_id=cycle.client_id)), cycle.period_start
    )
    subjects = {row.pk for row in obligations}
    rows = [
        row
        for row in observations
        if row.subject_kind == ObservationSubjectKind.OBLIGATION
        and row.subject_ref_id in subjects
        and row.kind in FACET_KINDS.values()
    ]
    return (
        _engagement_state(
            engagement,
            profile,
            {row.obligation_type_id: row for row in obligations},
            rows,
            timezone.now(),
        ),
    )


def load_cycle_detail(actor: "User", cycle_id: UUID) -> CycleDetailReads:
    """Load once for display, leaving decorators, navigation and commands live."""
    cycle = _cycle(actor, cycle_id)
    gates: tuple[str, ...] = (CYCLES_OPERATE, CYCLES_REVIEW, CYCLES_COMPLETE)
    if cycle.state in {CycleState.COMPLETED, CycleState.WITH_PENDING}:
        gates += (CYCLES_REOPEN,)
    levels = granted_levels(
        actor,
        gates,
        tenant_id=cycle.tenant_id,
    )
    requests = display_requests(cycle)
    proposals: QuerySet[WithAnnotations[ActionProposal, ProposalFacts]] = (
        cycle.proposals.annotate(
            display_executed=Exists(
                ActionExecution.objects.filter(proposal_id=OuterRef("pk"))
            )
        ).order_by("-created_at", "-pk")
    )
    links: QuerySet[WithAnnotations[EvidenceLink, LinkFacts]] = (
        EvidenceLink.objects.filter(request__cycle=cycle)
        .select_related("document")
        .annotate(
            intake_state=Subquery(
                DocumentIntake.objects.filter(document=OuterRef("document_id")).values(
                    "state"
                )[:1]
            )
        )
        .order_by("created_at", "pk")
    )
    obligations = tuple(
        Obligation.objects.filter(
            client_id=cycle.client_id, competence_month=cycle.period_start
        ).select_related("obligation_type")
    )
    observations = tuple(
        SourceObservation.objects.filter(
            Q(period=cycle.period_start) | Q(period__isnull=True),
            client_id=cycle.client_id,
        ).order_by("observed_at", "pk")
    )
    reads = CycleDetailReads(
        cycle=cycle,
        steps=tuple(cycle.playbook_version.steps.all()),
        tasks=tuple(cycle.tasks.select_related("owner")),
        notes=tuple(cycle.notes.select_related("author").order_by("created_at", "pk"))
        if cycle.display_has_notes
        else (),
        proposals=tuple(proposals) if cycle.display_has_proposals else (),
        requests=requests,
        links=tuple(links),
        exceptions=tuple(
            CycleException.objects.filter(cycle=cycle)
            .select_related("owner", "cycle__client", "cycle__engagement")
            .prefetch_related("internals")
            .order_by("due_on", "review_on", "created_at", "pk")
        )
        if cycle.display_has_exceptions
        else (),
        obligations=obligations,
        observations=observations,
        coverage=_coverage(cycle, obligations, observations),
        recipients=display_recipients(cycle.client),
        allowed=frozenset(
            slug for slug, level in levels.items() if level == GrantLevel.FULL
        ),
        valid_waivers=display_valid_waivers(cycle, requests),
        unresolved_messages=tuple(cycle.display_messages),
        index_current=False,
    )
    if cycle.display_index_digest is not None:
        return replace(
            reads,
            index_current=cycle.display_index_digest == display_source_digest(reads),
        )
    return reads
