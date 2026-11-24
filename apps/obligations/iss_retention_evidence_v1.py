"""Select and corroborate every scoped original that can state ISS retention."""

from __future__ import annotations

import hashlib
from contextlib import closing, suppress
from http import HTTPStatus
from typing import TYPE_CHECKING, Final, NamedTuple

from django.core.files.storage import default_storage
from storages.backends.s3 import S3Storage
from storages.utils import clean_name, safe_join

from apps.core.tasks import assert_no_transaction
from apps.cycles.evidence import CLEAN_STATES
from apps.cycles.extraction.v2 import MAX_BYTES
from apps.cycles.parsing import UnsafeDocument
from apps.obligations.iss_retention_v1 import (
    ISS_SOURCE_SPAN,
    PARTY_BY_CODE,
    corroborated_iss_code,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from uuid import UUID

    from django.db.backends.base.base import BaseDatabaseWrapper

EVIDENCE_UNAVAILABLE: Final = "evidence_unavailable"
EVIDENCE_DISAGREES: Final = "evidence_disagrees"

# CROSS_TENANT_OK: the maintenance pass names no tenant so it can page all heads.
# A live query names one exact revision/client/tenant and skips the page branch.
# Only preparation includes the incoming original before its attachment.
# The selected original ID (not the artifact's ID) is absent after loss or claim.
_ORIGINALS: Final = """
    WITH page AS (
        SELECT id, tenant_id, client_id, current_revision_id AS revision_id,
               iss_retention_party, fiscal_state, NULL::uuid AS incoming_id,
               NULL::text AS incoming_code
        FROM obligations_fiscaldocument
        WHERE %(tenant)s::uuid IS NULL
          AND (%(after)s::uuid IS NULL OR id > %(after)s::uuid)
        ORDER BY id LIMIT %(limit)s
    ), targets AS (
        SELECT * FROM page
        UNION ALL
        SELECT NULL::uuid, %(tenant)s::uuid, %(client)s::uuid, %(revision)s::uuid,
               NULL::varchar, NULL::varchar, %(incoming)s::uuid, %(code)s::text
        WHERE %(tenant)s::uuid IS NOT NULL
    )
    SELECT target.id, target.tenant_id, target.client_id, target.revision_id,
           target.iss_retention_party, target.fiscal_state,
           original.id, original.storage_key,
           original.sha256, original.byte_size,
           CASE WHEN intake.id IS NOT NULL THEN COALESCE(evidence.code,
               CASE WHEN refs.document_id = target.incoming_id AND NOT EXISTS (
                   SELECT 1 FROM cycles_extractedfield AS existing
                   WHERE existing.document_id = intake.document_id
                     AND existing.tenant_id = target.tenant_id
                     AND existing.client_id = target.client_id
                     AND existing.extractor_version = '2'
               ) THEN target.incoming_code END
           ) END
    FROM targets AS target
    LEFT JOIN LATERAL (
        SELECT artifact.document_id
        FROM obligations_revisionartifact AS artifact
        WHERE artifact.revision_id = target.revision_id
          AND artifact.tenant_id = target.tenant_id
          AND artifact.client_id = target.client_id
          AND artifact.role = 'xml_original'
        UNION SELECT target.incoming_id WHERE target.incoming_id IS NOT NULL
    ) AS refs ON true
    LEFT JOIN obligations_document AS original
      ON original.id = refs.document_id
     AND original.tenant_id = target.tenant_id AND original.client_id = target.client_id
     AND original.purged_at IS NULL
     AND NOT EXISTS (
        SELECT 1 FROM cycles_contentpurgeclaim AS claim
        WHERE claim.tenant_id = target.tenant_id AND claim.client_id = target.client_id
          AND claim.content_kind = 'document' AND claim.content_id = original.id
     )
    LEFT JOIN cycles_documentintake AS intake
      ON intake.document_id = original.id
     AND intake.tenant_id = target.tenant_id AND intake.client_id = target.client_id
     AND intake.state = ANY(%(clean_states)s)
    LEFT JOIN LATERAL (
        SELECT MIN(field.value) AS code
        FROM cycles_extractedfield AS field
        WHERE field.document_id = intake.document_id
          AND field.tenant_id = target.tenant_id AND field.client_id = target.client_id
          AND field.name = 'iss_withheld_flag'
          AND field.origin = 'xml' AND field.extractor_version = '2'
        HAVING COUNT(*) = 1
           AND BOOL_AND(field.redacted_at IS NULL AND field.source_span = %(span)s)
    ) AS evidence ON true
    ORDER BY target.id
"""


class ISSOriginal(NamedTuple):
    """Original identity plus the exact metadata authorized for reading its bytes."""

    document_id: UUID | None
    storage_key: str | None
    sha256: str | None
    byte_size: int | None
    code: str | None

    def usable(self) -> bool:
        """Require a documented source code before corroborating the bytes."""
        return self.code in PARTY_BY_CODE


class ISSOriginalRow(NamedTuple):
    """Exact maintenance target plus one original (or its unresolved placeholder)."""

    fiscal_id: UUID | None
    tenant_id: UUID
    client_id: UUID
    revision_id: UUID | None
    party: str | None
    state: str | None
    original: ISSOriginal


def _originals(
    db: BaseDatabaseWrapper,
    *,
    scope: tuple[UUID, UUID, UUID | None] | None = None,
    incoming: tuple[UUID, str | None] | None = None,
    page: tuple[UUID | None, int] = (None, 0),
) -> list[ISSOriginalRow]:
    tenant, client, revision = scope or (None, None, None)
    candidate, code = incoming or (None, None)
    after, limit = page
    with db.cursor() as cursor:
        cursor.execute(
            _ORIGINALS,
            {
                "tenant": tenant,
                "client": client,
                "revision": revision,
                "incoming": candidate,
                "code": code,
                "after": after,
                "limit": limit,
                "clean_states": sorted(CLEAN_STATES),
                "span": ISS_SOURCE_SPAN,
            },
        )
        return [
            ISSOriginalRow(
                fiscal_id,
                tenant_id,
                client_id,
                revision_id,
                party,
                state,
                ISSOriginal(document_id, storage_key, sha256, byte_size, code),
            )
            for (
                fiscal_id,
                tenant_id,
                client_id,
                revision_id,
                party,
                state,
                document_id,
                storage_key,
                sha256,
                byte_size,
                code,
            ) in cursor.fetchall()
        ]


def revision_iss_originals(
    db: BaseDatabaseWrapper,
    scope: tuple[UUID, UUID, UUID | None],
    *,
    incoming: tuple[UUID, str | None] | None = None,
) -> list[ISSOriginal]:
    """Snapshot the scoped revision's originals, optionally including a candidate."""
    return [row.original for row in _originals(db, scope=scope, incoming=incoming)]


def current_iss_originals_page(
    db: BaseDatabaseWrapper, after: UUID | None, limit: int
) -> list[ISSOriginalRow]:
    """Read one page of current heads and their original witnesses."""
    return _originals(db, page=(after, limit))


class ISSDecision(NamedTuple):
    """A verified code, insufficient source data, or a consistency cause."""

    code: str | None = None
    cause: str | None = None


def _read_retained_bytes(original: ISSOriginal) -> bytes | None:
    """Accept only a complete, bounded response matching the retained metadata."""
    key, size = original.storage_key, original.byte_size
    if key is None or size is None or not 0 < size <= MAX_BYTES:
        return None
    assert_no_transaction()
    # This boundary includes opening, streaming, and closing. Adapter exceptions
    # are not limited to the SDK hierarchy (e.g. a malformed HTTP range header).
    with suppress(Exception):
        storage = default_storage
        if isinstance(storage, S3Storage):
            # The managed download hides the HTTP status and may spool its entire
            # response before stream.read sees it. Request one unranged object.
            name = safe_join(storage.location, clean_name(key))
            response = storage.bucket.Object(name).get()
            with closing(response["Body"]) as stream:
                if (
                    response["ResponseMetadata"]["HTTPStatusCode"] != HTTPStatus.OK
                    or "ContentRange" in response
                    or response.get("ContentLength") != size
                ):
                    return None
                payload = stream.read(size + 1)
        else:
            with storage.open(key, "rb") as stream:
                payload = stream.read(size + 1)
        if (
            not isinstance(payload, bytes)
            or len(payload) != size
            or hashlib.sha256(payload).hexdigest() != original.sha256
        ):
            return None
        return payload
    return None


def verify_iss_original(original: ISSOriginal) -> ISSDecision:
    """Corroborate a retained token with its bounded, hash-matched vault bytes."""
    payload = _read_retained_bytes(original)
    if payload is None:
        return ISSDecision(cause=EVIDENCE_UNAVAILABLE)
    try:
        return ISSDecision(corroborated_iss_code(payload, original.code))
    except (UnsafeDocument, LookupError, ValueError):
        return ISSDecision(cause=EVIDENCE_UNAVAILABLE)


def agreed_iss_code(
    originals: Iterable[ISSOriginal],
    corroborate: Callable[[ISSOriginal], ISSDecision],
) -> ISSDecision:
    """Every current original must corroborate the same documented scalar."""
    codes: set[str] = set()
    unresolved = False
    for original in originals:
        verified = corroborate(original) if original.usable() else ISSDecision()
        if verified.cause:
            return verified
        if verified.code is None:
            unresolved = True
        else:
            codes.add(verified.code)
    if len(codes) > 1:
        return ISSDecision(cause=EVIDENCE_DISAGREES)
    return ISSDecision(None if unresolved else next(iter(codes), None))
