"""Batch the retained source-digest sections without changing frozen index writes."""

from datetime import datetime
from typing import TYPE_CHECKING, Final, TypedDict

from django.db.models import F, Value
from django.db.models.functions import JSONObject

from apps.cycles.effects.build import definition_hash
from apps.cycles.index_projection import digest_data
from apps.cycles.models import (
    CycleAmendment,
    CycleTask,
    DocumentIntake,
    EvidenceLink,
    EvidenceRequest,
    ExceptionInternal,
    ExtractedField,
    RequestInternal,
    Review,
)
from apps.cycles.models import Exception as CycleException
from apps.obligations.models import Document, SourceObservation

if TYPE_CHECKING:
    from django.db.models import Model, QuerySet

    from apps.cycles.display_reads import CycleDetailReads
    from apps.cycles.effects.manifest import JSONValue


# Whitelists are the retained sections of index_projection.source_digest.
# Equivalence tests compare the complete digest, including nonempty source histories.
SOURCE_FIELDS: Final = {
    "requests": (
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
        "title",
        "client_note",
    ),
    "request_decisions": ("id", "request_id", "version", "waiver_reason"),
    "links": (
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
    "documents": (
        "id",
        "sha256",
        "byte_size",
        "content_type",
        "uploaded_by_id",
        "created_at",
        "purged_at",
    ),
    "intakes": (
        "id",
        "document_id",
        "version",
        "state",
        "verdict",
        "scan_engine",
        "scan_signature_version",
        "scanned_at",
    ),
    "reviews": (
        "id",
        "link_id",
        "decision",
        "reviewer_id",
        "reviewed_at",
        "document_sha256",
        "third_party",
        "payment_relevant",
        "corrections_json",
        "override_reason",
    ),
    "fields": (
        "id",
        "document_id",
        "name",
        "origin",
        "extractor_version",
        "recorded_by_id",
        "created_at",
        "value",
        "source_span",
    ),
    "tasks": ("id", "step_key", "version", "state", "owner_id", "due_on"),
    "exceptions": (
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
    "exception_evidence": ("id", "exception_id", "evidence_refs"),
    "observations": (
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
        "source_ref",
        "value_json",
    ),
    "amendments": (
        "id",
        "number",
        "pinned_playbook_version_id",
        "affected_scope",
        "approved_by_id",
        "created_at",
        "reason",
        "completed_snapshot",
    ),
}
HASH_FIELDS: Final = {
    "requests": ("title", "client_note"),
    "request_decisions": ("waiver_reason",),
    "reviews": ("corrections_json", "override_reason"),
    "fields": ("value", "source_span"),
    "exception_evidence": ("evidence_refs",),
    "observations": ("source_ref", "value_json"),
    "amendments": ("reason", "completed_snapshot"),
}
DATETIME_FIELDS: Final = frozenset(
    {
        "created_at",
        "requested_at",
        "reviewed_at",
        "purged_at",
        "scanned_at",
        "observed_at",
        "recorded_at",
    }
)


class SourceRow(TypedDict):
    """One tagged, whitelisted JSON object from the union of scoped sources."""

    source_section: str
    source_payload: "dict[str, JSONValue]"


def _queries(reads: "CycleDetailReads") -> "dict[str, QuerySet[Model]]":
    cycle = reads.cycle
    scope = {"tenant_id": cycle.tenant_id, "client_id": cycle.client_id}
    requests = EvidenceRequest.objects.filter(**scope, cycle=cycle)
    links = EvidenceLink.objects.filter(**scope, request__cycle=cycle)
    documents = Document.objects.filter(**scope, pk__in=links.values("document_id"))
    selected = {
        identifier
        for proposal in reads.proposals
        for identifier in proposal.input_spec["observations"]
    }
    subjects = {row.obligation_id for row in reads.requests if row.obligation_id}
    subjects.update(
        row.subject_ref_id
        for row in reads.observations
        if row.period == cycle.period_start and str(row.pk) in selected
    )
    return {
        "requests": requests,
        "request_decisions": RequestInternal.objects.filter(
            **scope, request__in=requests
        ),
        "links": links,
        "documents": documents,
        "intakes": DocumentIntake.objects.filter(**scope, document__in=documents),
        "reviews": Review.objects.filter(**scope, link__in=links),
        "fields": ExtractedField.objects.filter(**scope, document__in=documents),
        "tasks": CycleTask.objects.filter(**scope, cycle=cycle),
        "exceptions": CycleException.objects.filter(**scope, cycle=cycle),
        "exception_evidence": ExceptionInternal.objects.filter(
            **scope, exception__cycle=cycle
        ),
        "observations": SourceObservation.objects.filter(
            **scope, period=cycle.period_start, subject_ref_id__in=subjects
        ),
        "amendments": CycleAmendment.objects.filter(**scope, cycle=cycle),
    }


def _tagged_rows(
    section: str, query: "QuerySet[Model]"
) -> "QuerySet[Model, SourceRow]":
    # Clear model ordering before UNION; final per-section ordering is by UUID.
    rows: QuerySet[Model, SourceRow] = (
        query.order_by()
        .annotate(
            source_section=Value(section),
            source_payload=JSONObject(
                **{name: F(name) for name in SOURCE_FIELDS[section]}
            ),
        )
        .values("source_section", "source_payload")
    )
    return rows


def _normalize(row: SourceRow) -> "dict[str, JSONValue]":
    payload = row["source_payload"]
    # PostgreSQL may trim fractional trailing zeroes in JSON timestamps. Restore
    # datetime.isoformat(), the exact normalization index_projection._value uses.
    data = {
        key: datetime.fromisoformat(value).isoformat()
        if key in DATETIME_FIELDS and isinstance(value, str)
        else value
        for key, value in payload.items()
    }
    for key in HASH_FIELDS.get(row["source_section"], ()):
        data[f"{key}_digest"] = digest_data(data.pop(key))
    return data


def display_source_digest(reads: "CycleDetailReads") -> str:
    """Compare all retained source bytes, never just a cycle/index revision."""
    queries = [
        _tagged_rows(section, query) for section, query in _queries(reads).items()
    ]
    sections: dict[str, list[JSONValue]] = {key: [] for key in SOURCE_FIELDS}
    rows = queries[0].union(*queries[1:], all=True)
    for row in sorted(
        rows, key=lambda row: (row["source_section"], str(row["source_payload"]["id"]))
    ):
        sections[row["source_section"]].append(_normalize(row))
    cycle = reads.cycle
    projection: dict[str, JSONValue] = {
        "schema": "axorim.evidence.v1",
        "playbook_hash": definition_hash(cycle.playbook_version),
        "cycle": {
            "id": str(cycle.pk),
            "amendment": cycle.amendment,
            "effect_revision": cycle.effect_revision,
        },
        **sections,
    }
    return digest_data(projection)
