"""Closed, client/cycle-scoped proof projections; content is represented by hashes."""

import hashlib
import json
from datetime import date
from uuid import UUID

from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Model, QuerySet

from apps.cycles.completion_sources import scoped_observations
from apps.cycles.effects.build import definition_hash
from apps.cycles.effects.manifest import JSONValue
from apps.cycles.models import (
    ActionExecution,
    ActionProposal,
    Approval,
    ClientAcknowledgement,
    ClientPublication,
    CompletionGeneration,
    CycleAmendment,
    CycleEvent,
    CycleTask,
    DocumentIntake,
    EvidenceLink,
    EvidenceRequest,
    ExceptionInternal,
    ExtractedField,
    RequestInternal,
    Review,
    ServiceCycle,
)
from apps.cycles.models import Exception as CycleException
from apps.delivery.models import DeliveryAttempt, DeliveryEffect, OutboundMessage
from apps.obligations.models import Document


def digest_data(data: JSONValue) -> str:
    """Hash a stable UTF-8 projection, not today's presentation markup."""
    return hashlib.sha256(
        json.dumps(
            data,
            cls=DjangoJSONEncoder,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _value(value: JSONValue | UUID | date) -> JSONValue:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


def _rows(
    queryset: QuerySet[Model], fields: tuple[str, ...], hashes: tuple[str, ...] = ()
) -> list[JSONValue]:
    """Only the explicit whitelists below enter the immutable index."""
    result: list[JSONValue] = []
    for row in queryset.order_by("pk").values(*fields, *hashes):
        projected = {key: _value(row[key]) for key in fields}
        projected.update({f"{key}_digest": digest_data(row[key]) for key in hashes})
        result.append(projected)
    return result


def collect_index(cycle: ServiceCycle) -> dict[str, JSONValue]:
    """Materialize one authorized cycle. No lazy relations or storage operations."""
    scope = {"tenant_id": cycle.tenant_id, "client_id": cycle.client_id}
    requests = EvidenceRequest.objects.filter(**scope, cycle=cycle)
    links = EvidenceLink.objects.filter(**scope, request__cycle=cycle)
    documents = Document.objects.filter(**scope, pk__in=links.values("document_id"))
    proposals = ActionProposal.objects.filter(**scope, cycle=cycle)
    messages = OutboundMessage.objects.filter(**scope, cycle=cycle)
    publications = ClientPublication.objects.filter(**scope, cycle=cycle)
    observations = scoped_observations(cycle)
    request_rows = _rows(
        requests,
        (
            "id",
            "version",
            "kind",
            "obligation_id",
            "required",
            "state",
            "due_on",
            "created_by_id",
            "created_at",
            "requested_at",
        ),
        ("title", "client_note"),
    )
    review_rows = _rows(
        Review.objects.filter(**scope, link__in=links),
        (
            "id",
            "link_id",
            "decision",
            "reviewer_id",
            "reviewed_at",
            "document_sha256",
            "third_party",
            "payment_relevant",
        ),
        ("corrections_json", "override_reason"),
    )
    fields = _rows(
        ExtractedField.objects.filter(**scope, document__in=documents),
        (
            "id",
            "document_id",
            "name",
            "origin",
            "extractor_version",
            "recorded_by_id",
            "created_at",
        ),
        ("value", "source_span"),
    )
    private_requests = _rows(
        RequestInternal.objects.filter(**scope, request__in=requests),
        (
            "id",
            "request_id",
            "version",
        ),
        ("waiver_reason",),
    )
    return {
        "schema": "axorim.evidence.v1",
        "playbook_hash": definition_hash(cycle.playbook_version),
        "cycle": {
            "id": str(cycle.pk),
            "tenant_id": str(cycle.tenant_id),
            "client_id": str(cycle.client_id),
            "engagement_id": str(cycle.engagement_id),
            "playbook_version_id": str(cycle.playbook_version_id),
            "tax_profile_id": str(cycle.tax_profile_id)
            if cycle.tax_profile_id
            else None,
            "version": cycle.version,
            "effect_revision": cycle.effect_revision,
            "amendment": cycle.amendment,
            "state": cycle.state,
            "period_start": cycle.period_start.isoformat(),
            "period_end_inclusive": cycle.period_end.isoformat(),
        },
        "requests": request_rows,
        "request_decisions": private_requests,
        "links": _rows(
            links,
            (
                "id",
                "request_id",
                "document_id",
                "version",
                "origin",
                "relinked",
                "submitted_by_id",
                "created_at",
                "review_decision",
                "reviewed_at",
            ),
        ),
        "documents": _rows(
            documents,
            (
                "id",
                "sha256",
                "byte_size",
                "content_type",
                "uploaded_by_id",
                "created_at",
                "purged_at",
            ),
        ),
        "intakes": _rows(
            DocumentIntake.objects.filter(**scope, document__in=documents),
            (
                "id",
                "document_id",
                "version",
                "state",
                "verdict",
                "scan_engine",
                "scan_signature_version",
                "scanned_at",
            ),
        ),
        "reviews": review_rows,
        "fields": fields,
        "tasks": _rows(
            CycleTask.objects.filter(**scope, cycle=cycle),
            (
                "id",
                "step_key",
                "version",
                "state",
                "owner_id",
                "due_on",
            ),
        ),
        "exceptions": _rows(
            CycleException.objects.filter(**scope, cycle=cycle),
            (
                "id",
                "version",
                "subject_ref",
                "reason_code",
                "state",
                "owner_id",
                "due_on",
                "review_on",
                "blocks_completion",
            ),
        ),
        "exception_evidence": _rows(
            ExceptionInternal.objects.filter(**scope, exception__cycle=cycle),
            ("id", "exception_id"),
            ("evidence_refs",),
        ),
        "proposals": _rows(
            proposals,
            (
                "id",
                "revision",
                "version",
                "action_kind",
                "digest",
                "state",
                "previous_id",
                "initiator_id",
                "business_intent_key",
                "created_at",
                "expires_at",
            ),
        ),
        "approvals": _rows(
            Approval.objects.filter(**scope, proposal__in=proposals),
            (
                "id",
                "proposal_id",
                "proposal_revision",
                "digest",
                "approver_id",
                "membership_id",
                "membership_version",
                "decision",
                "decided_at",
                "expires_at",
            ),
        ),
        "executions": _rows(
            ActionExecution.objects.filter(**scope, cycle=cycle),
            (
                "id",
                "proposal_id",
                "proposal_revision",
                "digest",
                "business_intent_key",
                "action_kind",
                "executed_by_id",
                "executed_at",
            ),
        ),
        "messages": _rows(
            messages,
            (
                "id",
                "version",
                "proposal_id",
                "evidence_request_id",
                "kind",
                "content_digest",
                "state",
                "provider_message_id",
                "created_at",
            ),
        ),
        "provider_responses": _rows(
            DeliveryAttempt.objects.filter(**scope, message__in=messages),
            (
                "id",
                "message_id",
                "attempt_id",
                "fence",
                "outcome",
                "error_code",
                "actor_id",
                "note_digest",
                "occurred_at",
            ),
        ),
        "delivery_effects": _rows(
            DeliveryEffect.objects.filter(**scope, message__in=messages),
            (
                "id",
                "message_id",
                "business_intent_key",
                "content_digest",
                "outcome",
                "occurred_at",
            ),
        ),
        "publications": _rows(
            publications,
            (
                "id",
                "proposal_id",
                "proposal_revision",
                "publication_digest",
                "version",
                "state",
                "published_at",
                "withdrawn_at",
                "supersedes_id",
            ),
        ),
        "acknowledgements": _rows(
            ClientAcknowledgement.objects.filter(**scope, publication__in=publications),
            (
                "id",
                "publication_id",
                "publication_digest",
                "user_id",
                "acknowledged_at",
            ),
        ),
        "observations": _rows(
            observations,
            (
                "id",
                "subject_kind",
                "subject_ref_id",
                "kind",
                "period",
                "source_kind",
                "verification_status",
                "outcome",
                "observed_at",
                "recorded_at",
                "recorded_by_id",
                "supersedes_id",
                "evidence_link_id",
            ),
            ("source_ref", "value_json"),
        ),
        "amendments": _rows(
            CycleAmendment.objects.filter(**scope, cycle=cycle),
            (
                "id",
                "number",
                "pinned_playbook_version_id",
                "affected_scope",
                "approved_by_id",
                "created_at",
            ),
            ("reason", "completed_snapshot"),
        ),
        "completions": _rows(
            CompletionGeneration.objects.filter(**scope, cycle=cycle),
            (
                "id",
                "proposal_id",
                "index_id",
                "amendment",
                "cycle_revision",
                "digest",
                "completed_at",
            ),
        ),
        "events": _rows(
            CycleEvent.objects.filter(**scope, cycle=cycle),
            (
                "id",
                "type",
                "schema_version",
                "aggregate_type",
                "aggregate_id",
                "aggregate_version",
                "actor_id",
                "occurred_at",
                "observed_at",
                "correlation_id",
                "causation_id",
            ),
        ),
    }


def source_digest(cycle: ServiceCycle) -> str:
    """Freshness excludes bookkeeping which proposing/completing itself creates."""
    projection = collect_index(cycle)
    projection["cycle"] = {
        "id": str(cycle.pk),
        "amendment": cycle.amendment,
        "effect_revision": cycle.effect_revision,
    }
    for key in (
        "proposals",
        "approvals",
        "executions",
        "events",
        "messages",
        "provider_responses",
        "delivery_effects",
        "publications",
        "acknowledgements",
        "completions",
    ):
        del projection[key]
    return digest_data(projection)
