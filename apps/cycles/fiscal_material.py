"""One versioned material digest for fiscal review writes and set-based readers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, TypedDict

from django.contrib.postgres.expressions import ArraySubquery
from django.db.models import (
    BinaryField,
    Case,
    CharField,
    DateField,
    F,
    Func,
    OuterRef,
    TextField,
    Value,
    When,
)
from django.db.models.functions import Cast, Collate, JSONArray

from apps.obligations.models import RevisionArtifact

if TYPE_CHECKING:
    from django.db.models import QuerySet
    from django_stubs_ext import WithAnnotations

    from apps.obligations.models import FiscalDocument

# The tag is hashed, so fingerprints retain the existing 64-hex storage contract.
# A legacy hash of provider_version:original_sha does not match this encoding.
REVIEW_MATERIAL_VERSION: Final = "fiscal-review-material:v1"


class Material(TypedDict):
    """Current revision material computed by the document statement."""

    review_material: str


def snapshot_rows(
    rows: QuerySet[FiscalDocument],
) -> QuerySet[WithAnnotations[FiscalDocument, Material]]:
    """Bind the material to its current revision; narrower readers can project it."""
    return rows.select_related("current_revision").annotate(
        review_material=material_digest()
    )


def working_date() -> Case:
    """Use issue date for NF-e and competence for NFS-e."""
    return Case(
        When(family="nfe", then=F("issue_date")),
        default=F("competence"),
        output_field=DateField(),
    )


def material_digest() -> Func:
    """Project the v1 digest on a FiscalDocument in the same SQL snapshot.

    The ordered JSON array is unambiguous even for arbitrary provider strings.
    Include only retained artifact roles/bytes, not their row IDs or times.
    """
    artifacts = (
        RevisionArtifact.objects.filter(
            revision_id=OuterRef("current_revision_id"),
            document__purged_at__isnull=True,
        )
        .values(
            material=Collate(
                Cast(JSONArray("role", "document__sha256"), TextField()), "C"
            )
        )
        .order_by("material")
        .distinct()
    )
    material = JSONArray(
        Value(REVIEW_MATERIAL_VERSION),
        "current_revision_id",
        "current_revision__semantic_digest",
        "fiscal_state",
        "summary_only",
        "environment",
        "family",
        "key_namespace",
        "document_key",
        "client_id",
        "client_role",
        "role_verification",
        "counterpart_id",
        "counterpart_name",
        working_date(),
        "issue_date",
        "competence",
        "municipality_incidence",
        "total",
        "iss_stated",
        "iss_retention_party",
        "iss_withheld_flag",
        "iss_withheld_amount",
        ArraySubquery(artifacts),
    )
    return sha256_hex(material)


def sha256_hex(value: Func) -> Func:
    """Canonical text → UTF-8 → hex SHA-256 without a database extension."""
    utf8 = Func(Cast(value, TextField()), Value("UTF8"), function="CONVERT_TO")
    hashed = Func(utf8, function="SHA256", output_field=BinaryField())
    return Func(hashed, Value("hex"), function="ENCODE", output_field=CharField())
