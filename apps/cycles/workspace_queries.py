"""One authorized client-period aggregate for the populated staff workspace.

Ordered JSON arrays avoid multiplying heterogeneous children in a join. The SQL
boundary restores typed model rows through Django's public deserializer, using
scalar foreign keys only (no natural-key reads, saves, or deferred fields).
Standalone module readers keep their own authorization and query contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Final, TypedDict
from uuid import UUID

from django.contrib.postgres.expressions import ArraySubquery
from django.core.serializers import deserialize
from django.db.models import Exists, OuterRef, Q, Value
from django.db.models.functions import JSONObject
from django.http import Http404
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import Event
from apps.authz.models import GrantLevel
from apps.authz.portfolio import (
    VIEW_ALL,
    VIEW_ASSIGNED,
    assignable_members,
    clients_in_scope,
    scope_for_levels,
)
from apps.authz.services import granted_levels_with_role
from apps.authz.support import in_support_mode
from apps.clients.maintenance import MANAGE_CAPABILITY
from apps.clients.models import (
    ClientCompany,
    ClientContact,
    Engagement,
    OnboardingItem,
    TaxProfile,
)
from apps.clients.services import claim_at
from apps.cycles.capabilities import CYCLES_COMPLETE, CYCLES_OPERATE, CYCLES_REVIEW
from apps.cycles.evidence import counted_links
from apps.cycles.exception_context import ExceptionControls
from apps.cycles.models import (
    CycleEvent,
    EvidenceRequest,
    Playbook,
    PlaybookVersion,
    ServiceCycle,
    VersionState,
)
from apps.cycles.rules import next_period_start
from apps.cycles.services.cycle import require_enabled, tenant_id
from apps.cycles.workspace import (
    EVENT_LABELS,
    OUTBOX_TIMELINE_READERS,
    CycleContext,
    TimelineEntry,
    actions_for,
)
from apps.cycles.workspace_sections import PeriodRequest, project_request
from apps.delivery.timeline import PeriodCommunication, period_communication
from apps.obligations.coverage import (
    FACET_KINDS,
    CoverageInputs,
    CoverageItem,
    coverage_from_rows,
)
from apps.obligations.models import (
    Document,
    Obligation,
    ObligationType,
    ObservationSubjectKind,
    SourceObservation,
)
from apps.tenants.models import Membership

if TYPE_CHECKING:
    from django.db.models import Model, QuerySet
    from django_stubs_ext import WithAnnotations

type JsonValue = (
    str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None
)


class ModelRow(TypedDict):
    """Django's scalar-key Python serialization shape at the SQL boundary."""

    model: str
    pk: str | None
    fields: dict[str, JsonValue]


class PersonRow(TypedDict):
    """Only the identity and email needed for a staff display label."""

    id: str | None
    email: str | None


class EngagementRow(TypedDict):
    """A complete engagement and its already joined display person."""

    row: ModelRow
    responsible: PersonRow


class CycleRow(TypedDict):
    """A complete cycle with all relations used by context and rendering."""

    row: ModelRow
    playbook: ModelRow
    definition: ModelRow
    engagement: ModelRow
    owner: PersonRow
    responsible: PersonRow


class DefinitionRow(TypedDict):
    """The latest published definition of each playbook."""

    row: ModelRow
    playbook: ModelRow


class ObligationRow(TypedDict):
    """An obligation with its reference type, without a lazy label read."""

    row: ModelRow
    obligation_type: ModelRow


class RequestRow(TypedDict):
    """All inputs of T43's typed request projector, including counted evidence."""

    row: ModelRow
    obligation: ModelRow
    obligation_type: ModelRow
    submitted: bool


class MemberRow(TypedDict):
    """An authorized exception roster seat and its display person."""

    row: ModelRow
    user: PersonRow


class CycleHistoryRow(TypedDict):
    """Reference-only event facts, excluding the event payload."""

    id: str
    occurred_at: str
    type: str
    actor_id: str | None
    aggregate_id: str


class AuditHistoryRow(TypedDict):
    """Reference-only audit facts, excluding the audit metadata."""

    id: str
    created_at: str
    object_id: str


class WorkspaceArrays(TypedDict):
    """The annotation types on the single authorized ClientCompany row."""

    workspace_cycles: list[CycleRow]
    workspace_engagements: list[EngagementRow]
    workspace_profiles: list[ModelRow]
    workspace_obligations: list[ObligationRow]
    workspace_observations: list[ModelRow]
    workspace_definitions: list[DefinitionRow]
    workspace_documents: list[ModelRow]
    workspace_contacts: list[ModelRow]
    workspace_checklist: list[ModelRow]
    workspace_requests: list[RequestRow]
    workspace_roster: list[MemberRow]
    workspace_events: list[CycleHistoryRow]
    workspace_audits: list[AuditHistoryRow]


@dataclass(frozen=True, slots=True)
class WorkspaceRead:
    """Typed, request-local facts; no authorization result survives this read."""

    client: ClientCompany
    period: date
    context: CycleContext
    cycles: list[ServiceCycle]
    engagements: list[Engagement]
    definitions: list[PlaybookVersion]
    documents: list[Document]
    obligations: list[Obligation]
    coverage: list[CoverageItem]
    contacts: list[ClientContact]
    checklist: list[OnboardingItem]
    requests: list[PeriodRequest]
    communication: PeriodCommunication
    timeline: list[TimelineEntry]
    allowed: frozenset[str]
    exception_controls: ExceptionControls


# Complete scalar model fields, not a second set of display rules. Foreign-key
# names deliberately omit "_id": the deserializer converts the scalar keys using
# the real fields, including UUIDs, dates, timestamps and nullable relations.
ENGAGEMENT_FIELDS: Final = (
    "tenant",
    "client",
    "service_kind",
    "starts_on",
    "ends_on",
    "responsible",
    "provenance",
    "version",
    "created_at",
)
PROFILE_FIELDS: Final = (
    "tenant",
    "client",
    "regime",
    "mei_category",
    "valid_from",
    "valid_to",
    "provenance",
    "confidence",
    "recorded_by",
    "recorded_at",
    "superseded_at",
    "correction_of",
    "note",
)
CYCLE_FIELDS: Final = (
    "tenant",
    "client",
    "engagement",
    "playbook",
    "playbook_version",
    "tax_profile",
    "period_start",
    "period_end",
    "state",
    "owner",
    "reviewer",
    "version",
    "effect_revision",
    "amendment",
    "created_at",
)
PLAYBOOK_FIELDS: Final = ("tenant", "key", "name", "is_system", "created_at")
DEFINITION_FIELDS: Final = (
    "tenant",
    "playbook",
    "number",
    "state",
    "published_at",
    "published_by",
    "applies_to_json",
    "version",
    "created_at",
)
OBLIGATION_FIELDS: Final = (
    "tenant",
    "client",
    "obligation_type",
    "competence_month",
    "nominal_due_date",
    "resolved_due_date",
    "status",
    "evidence_ref",
    "created_at",
    "updated_at",
)
OBLIGATION_TYPE_FIELDS: Final = ("name", "periodicity", "source_note", "source_url")
OBSERVATION_FIELDS: Final = (
    "tenant",
    "client",
    "subject_kind",
    "subject_ref",
    "kind",
    "period",
    "source_kind",
    "observed_at",
    "value_json",
    "verification_status",
    "supersedes",
    "recorded_by",
    "outcome",
    "source_ref",
    "evidence_link",
    "recorded_at",
)
DOCUMENT_FIELDS: Final = (
    "tenant",
    "client",
    "obligation",
    "storage_key",
    "sha256",
    "original_filename",
    "content_type",
    "byte_size",
    "uploaded_by",
    "created_at",
    "client_upload",
    "retention_class",
    "purged_at",
)
CONTACT_FIELDS: Final = (
    "tenant",
    "client",
    "name",
    "email",
    "phone",
    "role_label",
    "membership",
    "channel_email_allowed",
    "is_primary",
    "active",
    "version",
    "created_at",
)
CHECKLIST_FIELDS: Final = (
    "tenant",
    "client",
    "key",
    "label",
    "position",
    "requires_ecac",
    "status",
    "blocked_reason",
    "completed_at",
)
REQUEST_FIELDS: Final = (
    "tenant",
    "client",
    "cycle",
    "kind",
    "obligation",
    "title",
    "client_note",
    "due_on",
    "required",
    "state",
    "created_by",
    "requested_at",
    "version",
    "created_at",
)
MEMBER_FIELDS: Final = (
    "tenant",
    "user",
    "client",
    "role",
    "is_active",
    "review_grant_version",
    "version",
    "created_at",
)


def _record(label: str, fields: tuple[str, ...], prefix: str = "") -> JSONObject:
    """Build scalar-key model data without reading or exposing private metadata."""
    return JSONObject(
        model=Value(label),
        pk=f"{prefix}pk",
        fields=JSONObject(**{name: f"{prefix}{name}" for name in fields}),
    )


def _array(rows: QuerySet[Model], data: JSONObject) -> ArraySubquery:
    """Preserve each source's ordering and limits inside its own array."""
    return ArraySubquery(rows.values(data=data))


def _restore[M: Model](model: type[M], row: ModelRow) -> M:
    """Parse SQL JSON through the real fields, without saving the resulting row."""
    restored = next(deserialize("python", [row])).object
    if not isinstance(restored, model):
        message = f"Workspace row names {row['model']}, expected {model.__name__}."
        raise TypeError(message)
    return restored


def _person(row: PersonRow) -> User | None:
    """Bind a display person without a subsequent user lookup."""
    if row["id"] is None:
        return None
    email = row["email"]
    if email is None:
        message = "A joined workspace person requires a non-null email."
        raise TypeError(message)
    return User(id=UUID(row["id"]), email=email)


def _cycle_source(period: date) -> ArraySubquery:
    rows = ServiceCycle.objects.filter(
        client_id=OuterRef("pk"), period_start=period
    ).order_by("created_at", "pk")
    return _array(
        rows,
        JSONObject(
            row=_record("cycles.servicecycle", CYCLE_FIELDS),
            playbook=_record("cycles.playbook", PLAYBOOK_FIELDS, "playbook__"),
            definition=_record(
                "cycles.playbookversion", DEFINITION_FIELDS, "playbook_version__"
            ),
            engagement=_record("clients.engagement", ENGAGEMENT_FIELDS, "engagement__"),
            owner=JSONObject(id="owner_id", email="owner__email"),
            responsible=JSONObject(
                id="engagement__responsible_id", email="engagement__responsible__email"
            ),
        ),
    )


def _coverage_sources(period: date) -> dict[str, ArraySubquery]:
    engagements = (
        Engagement.objects.filter(client_id=OuterRef("pk"), starts_on__lte=period)
        .filter(Q(ends_on__isnull=True) | Q(ends_on__gt=period))
        .order_by("pk")
    )
    obligations = Obligation.objects.filter(
        client_id=OuterRef("pk"), competence_month=period
    ).order_by("competence_month", "pk")
    observations = (
        SourceObservation.objects.filter(
            client_id=OuterRef("pk"),
            subject_kind=ObservationSubjectKind.OBLIGATION,
            subject_ref__competence_month=period,
            kind__in=set(FACET_KINDS.values()),
        )
        .filter(Q(period=period) | Q(period__isnull=True))
        .order_by("observed_at", "pk")
    )
    return {
        "workspace_engagements": _array(
            engagements,
            JSONObject(
                row=_record("clients.engagement", ENGAGEMENT_FIELDS),
                responsible=JSONObject(id="responsible_id", email="responsible__email"),
            ),
        ),
        "workspace_profiles": _array(
            TaxProfile.objects.filter(client_id=OuterRef("pk")).order_by("pk"),
            _record("clients.taxprofile", PROFILE_FIELDS),
        ),
        "workspace_obligations": _array(
            obligations,
            JSONObject(
                row=_record("obligations.obligation", OBLIGATION_FIELDS),
                obligation_type=_record(
                    "obligations.obligationtype",
                    OBLIGATION_TYPE_FIELDS,
                    "obligation_type__",
                ),
            ),
        ),
        "workspace_observations": _array(
            observations, _record("obligations.sourceobservation", OBSERVATION_FIELDS)
        ),
    }


def _registry_sources() -> dict[str, ArraySubquery]:
    definitions = (
        PlaybookVersion.objects.filter(state=VersionState.PUBLISHED)
        .order_by("playbook_id", "-number")
        .distinct("playbook_id")
    )
    return {
        "workspace_definitions": _array(
            definitions,
            JSONObject(
                row=_record("cycles.playbookversion", DEFINITION_FIELDS),
                playbook=_record("cycles.playbook", PLAYBOOK_FIELDS, "playbook__"),
            ),
        ),
        "workspace_documents": _array(
            Document.objects.filter(client_id=OuterRef("pk")).order_by(
                "-created_at", "pk"
            )[:20],
            _record("obligations.document", DOCUMENT_FIELDS),
        ),
        "workspace_contacts": _array(
            ClientContact.objects.filter(
                client_id=OuterRef("pk"), active=True
            ).order_by("pk"),
            _record("clients.clientcontact", CONTACT_FIELDS),
        ),
        "workspace_checklist": _array(
            OnboardingItem.objects.filter(client_id=OuterRef("pk")).order_by(
                "position", "pk"
            ),
            _record("clients.onboardingitem", CHECKLIST_FIELDS),
        ),
    }


def _request_source(period: date) -> ArraySubquery:
    rows = (
        EvidenceRequest.objects.filter(
            client_id=OuterRef("pk"), cycle__period_start=period
        )
        .annotate(submitted=Exists(counted_links().filter(request_id=OuterRef("pk"))))
        .order_by("created_at", "pk")
    )
    return _array(
        rows,
        JSONObject(
            row=_record("cycles.evidencerequest", REQUEST_FIELDS),
            obligation=_record(
                "obligations.obligation", OBLIGATION_FIELDS, "obligation__"
            ),
            obligation_type=_record(
                "obligations.obligationtype",
                OBLIGATION_TYPE_FIELDS,
                "obligation__obligation_type__",
            ),
            submitted="submitted",
        ),
    )


def _history_sources(client_id: UUID, period: date) -> dict[str, ArraySubquery]:
    events = CycleEvent.objects.filter(
        client_id=OuterRef("pk"), cycle__period_start=period
    ).order_by("-occurred_at", "-pk")[:100]
    audits = Event.objects.filter(
        Q(metadata__client_id=str(client_id))
        | Q(
            object_type__in=["ClientCompany", "clients.ClientCompany"],
            object_id=str(client_id),
        ),
        created_at__date__gte=period,
        created_at__date__lt=next_period_start(period),
    ).order_by("-created_at", "-pk")[:100]
    return {
        "workspace_events": _array(
            events,
            JSONObject(
                id="pk",
                occurred_at="occurred_at",
                type="type",
                actor_id="actor_id",
                aggregate_id="aggregate_id",
            ),
        ),
        "workspace_audits": _array(
            audits, JSONObject(id="pk", created_at="created_at", object_id="object_id")
        ),
    }


def _aggregate(
    clients: QuerySet[ClientCompany], client_id: UUID, period: date
) -> QuerySet[WithAnnotations[ClientCompany, WorkspaceArrays]]:
    members = assignable_members(OuterRef(OuterRef("pk")), tenant_id=tenant_id())
    coverage = _coverage_sources(period)
    registry = _registry_sources()
    history = _history_sources(client_id, period)
    return clients.filter(pk=client_id, tenant_id=tenant_id()).annotate(
        workspace_cycles=_cycle_source(period),
        workspace_requests=_request_source(period),
        workspace_roster=_array(
            members,
            JSONObject(
                row=_record("tenants.membership", MEMBER_FIELDS),
                user=JSONObject(id="user_id", email="user__email"),
            ),
        ),
        workspace_engagements=coverage["workspace_engagements"],
        workspace_profiles=coverage["workspace_profiles"],
        workspace_obligations=coverage["workspace_obligations"],
        workspace_observations=coverage["workspace_observations"],
        workspace_definitions=registry["workspace_definitions"],
        workspace_documents=registry["workspace_documents"],
        workspace_contacts=registry["workspace_contacts"],
        workspace_checklist=registry["workspace_checklist"],
        workspace_events=history["workspace_events"],
        workspace_audits=history["workspace_audits"],
    )


def _cycles(rows: list[CycleRow]) -> list[ServiceCycle]:
    result = []
    for payload in rows:
        cycle = _restore(ServiceCycle, payload["row"])
        cycle.playbook = _restore(Playbook, payload["playbook"])
        cycle.playbook_version = _restore(PlaybookVersion, payload["definition"])
        cycle.engagement = _restore(Engagement, payload["engagement"])
        cycle.engagement.responsible = _person(payload["responsible"])
        cycle.owner = _person(payload["owner"])
        result.append(cycle)
    return result


def _definitions(rows: list[DefinitionRow]) -> list[PlaybookVersion]:
    result = []
    for payload in rows:
        definition = _restore(PlaybookVersion, payload["row"])
        definition.playbook = _restore(Playbook, payload["playbook"])
        result.append(definition)
    return result


def _engagements(rows: list[EngagementRow]) -> list[Engagement]:
    result = []
    for payload in rows:
        engagement = _restore(Engagement, payload["row"])
        engagement.responsible = _person(payload["responsible"])
        result.append(engagement)
    return result


def _obligations(rows: list[ObligationRow]) -> list[Obligation]:
    result = []
    for payload in rows:
        obligation = _restore(Obligation, payload["row"])
        obligation.obligation_type = _restore(
            ObligationType, payload["obligation_type"]
        )
        result.append(obligation)
    return result


def _requests(
    rows: list[RequestRow], cycles: list[ServiceCycle], today: date
) -> list[PeriodRequest]:
    by_id = {cycle.pk: cycle for cycle in cycles}
    result = []
    for payload in rows:
        row = _restore(EvidenceRequest, payload["row"])
        row.cycle = by_id[row.cycle_id]
        if payload["obligation"]["pk"] is None:
            row.obligation = None
        else:
            obligation = _restore(Obligation, payload["obligation"])
            obligation.obligation_type = _restore(
                ObligationType, payload["obligation_type"]
            )
            row.obligation = obligation
        result.append(project_request(row, today=today, submitted=payload["submitted"]))
    return result


def _roster(rows: list[MemberRow]) -> list[Membership]:
    result = []
    for payload in rows:
        member = _restore(Membership, payload["row"])
        person = _person(payload["user"])
        if person is None:
            message = "An active workspace roster seat requires a user."
            raise TypeError(message)
        member.user = person
        result.append(member)
    return result


def _timeline(
    row: WithAnnotations[ClientCompany, WorkspaceArrays], period: date
) -> list[TimelineEntry]:
    entries = [
        TimelineEntry(
            UUID(event["id"]),
            datetime.fromisoformat(event["occurred_at"]),
            EVENT_LABELS.get(event["type"], "Evento do ciclo"),
            "Ação da equipe" if event["actor_id"] else "Sistema / agenda",
            event["aggregate_id"],
        )
        for event in row.workspace_events
    ]
    entries.extend(
        TimelineEntry(
            UUID(audit["id"]),
            datetime.fromisoformat(audit["created_at"]),
            "Registro de auditoria",
            "Auditoria / equipe",
            audit["object_id"],
        )
        for audit in row.workspace_audits
    )
    # Keep the receipt-driven window and every registered source, independently
    # of period_communication's newest-message page and all of its receipts.
    for reader in OUTBOX_TIMELINE_READERS:
        entries.extend(reader(row.tenant_id, row.pk, period))
    return sorted(entries, key=lambda entry: (entry.when, entry.id), reverse=True)[:100]


def _context(
    client: ClientCompany,
    period: date,
    cycles: list[ServiceCycle],
    profiles: list[TaxProfile],
    allowed: frozenset[str],
) -> CycleContext:
    cycle = min(cycles, key=lambda row: (row.engagement_id, row.pk)) if cycles else None
    return CycleContext(
        tenant_id=client.tenant_id,
        client_id=client.pk,
        engagement_id=cycle.engagement_id if cycle else None,
        period_start=period,
        cycle_id=cycle.pk if cycle else None,
        cycle_state=cycle.state if cycle else None,
        version=cycle.version if cycle else None,
        playbook_version=(cycle.playbook_version_id, cycle.playbook_version.number)
        if cycle
        else None,
        tax_profile=claim_at(profiles, period),
        coverage=("unknown",),
        available_actions=actions_for(cycle, allowed) if cycle else (),
    )


def read_workspace(actor: User, client_id: UUID, period: date) -> WorkspaceRead:
    """Resolve the live portfolio and controls once, then read the full period."""
    require_enabled()
    role, levels = granted_levels_with_role(
        actor,
        (
            VIEW_ASSIGNED,
            VIEW_ALL,
            CYCLES_OPERATE,
            CYCLES_REVIEW,
            CYCLES_COMPLETE,
            MANAGE_CAPABILITY,
        ),
        tenant_id=tenant_id(),
    )
    clients = clients_in_scope(actor, scope_for_levels(role, levels))
    row = _aggregate(clients, client_id, period).first()
    if row is None:
        raise Http404
    allowed = frozenset(
        slug for slug, level in levels.items() if level == GrantLevel.FULL
    )
    cycles = _cycles(row.workspace_cycles)
    engagements = _engagements(row.workspace_engagements)
    profiles = [_restore(TaxProfile, payload) for payload in row.workspace_profiles]
    obligations = _obligations(row.workspace_obligations)
    observations = [
        _restore(SourceObservation, payload) for payload in row.workspace_observations
    ]
    moment = timezone.now()
    return WorkspaceRead(
        client=row,
        period=period,
        context=_context(row, period, cycles, profiles, allowed),
        cycles=cycles,
        engagements=engagements,
        definitions=_definitions(row.workspace_definitions),
        documents=[_restore(Document, payload) for payload in row.workspace_documents],
        obligations=obligations,
        coverage=coverage_from_rows(
            CoverageInputs(engagements, profiles, obligations, observations),
            period,
            now=moment,
        ),
        contacts=[
            _restore(ClientContact, payload) for payload in row.workspace_contacts
        ],
        checklist=[
            _restore(OnboardingItem, payload) for payload in row.workspace_checklist
        ],
        requests=_requests(
            row.workspace_requests, cycles, timezone.localtime(moment).date()
        ),
        communication=period_communication(row.pk, period, redact=in_support_mode()),
        timeline=_timeline(row, period),
        allowed=allowed,
        exception_controls=ExceptionControls(
            _roster(row.workspace_roster), CYCLES_REVIEW in allowed
        ),
    )
