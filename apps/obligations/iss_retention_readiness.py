"""Proof that a scoped document's current ISS label has been projected."""

import hashlib
import json
from collections.abc import Iterable
from typing import Final
from uuid import UUID

from django.db import connection
from django.db.models import QuerySet
from psycopg.sql import SQL

from apps.audit.models import Event
from apps.audit.services import ObjectRef, record_event
from apps.core.tenancy import current_tenant_id
from apps.cycles.evidence import CLEAN_STATES
from apps.obligations.iss_retention_evidence_v1 import (
    _ORIGINALS,
    ISSDecision,
    ISSOriginal,
)
from apps.obligations.iss_retention_v1 import ISS_SOURCE_SPAN
from apps.obligations.models.fiscal import FiscalDocument

_READY: Final = "fiscal.iss_party_ready"

# A request transaction at READ COMMITTED does not give separate SELECTs
# one snapshot. Compare the rendered head and read its witnesses and marks
# in one statement, without blocking a writer or contacting the vault.
_CURRENT_READINESS: Final = (
    SQL("""
    WITH originals (
        fiscal_id, tenant_id, client_id, revision_id, party, state,
        document_id, storage_key, sha256, byte_size, code
    ) AS ({originals})
    SELECT (
        SELECT json_agg(
            json_build_array(document_id, storage_key, sha256, byte_size, code)
        ) FROM originals WHERE document_id IS NOT NULL
    )::text,
    ARRAY(
        SELECT mark.metadata::text
        FROM audit_event AS mark
        WHERE mark.tenant_id = head.tenant_id
          AND mark.action = %(action)s
          AND mark.object_type = 'FiscalDocument'
          AND mark.object_id = head.id::text
          AND mark.metadata->>'client_id' = head.client_id::text
          AND mark.metadata->>'revision_id' = %(revision)s::uuid::text
    )
    FROM obligations_fiscaldocument AS head
    WHERE head.id = %(fiscal)s AND head.tenant_id = %(tenant)s
      AND head.client_id = %(client)s
      AND (head.current_revision_id, head.version, head.family,
           head.fiscal_state, head.iss_retention_party)
        = (%(revision)s::uuid, %(version)s, %(family)s, %(state)s, %(party)s)
""")
    .format(originals=SQL(_ORIGINALS))
    .as_string()
)


def _can_have_label(fiscal: FiscalDocument) -> bool:
    return (
        fiscal.family == "nfse_nacional"
        and fiscal.current_revision_id is not None
        and fiscal.fiscal_state != "incerta"
    )


def _stamp(
    fiscal: FiscalDocument, originals: Iterable[ISSOriginal]
) -> dict[str, object]:
    # Same-head attachments and redaction/purge can change the inputs without
    # moving the revision or bumping the fiscal version. Store only their digest.
    snapshot = [
        fiscal.version,
        fiscal.iss_retention_party,
        sorted(
            (str(w.document_id), w.sha256, w.byte_size, w.storage_key, w.code)
            for w in originals
            if w.document_id is not None
        ),
    ]
    return {
        "client_id": str(fiscal.client_id),
        "revision_id": str(fiscal.current_revision_id),
        "snapshot": hashlib.sha256(json.dumps(snapshot).encode()).hexdigest(),
    }


def _marks(fiscal: FiscalDocument) -> QuerySet[Event]:
    return Event.objects.filter(
        tenant_id=fiscal.tenant_id,
        action=_READY,
        object_type="FiscalDocument",
        object_id=str(fiscal.pk),
    )


def record_iss_party_ready(
    fiscal: FiscalDocument, decision: ISSDecision, originals: Iterable[ISSOriginal]
) -> None:
    """Certify a completed projection in the same transaction as the locked head."""
    if decision.cause is not None or not _can_have_label(fiscal):
        return
    stamp = _stamp(fiscal, originals)
    if not _marks(fiscal).filter(metadata=stamp).exists():
        record_event(
            tenant_id=fiscal.tenant_id,
            action=_READY,
            obj=ObjectRef("FiscalDocument", str(fiscal.pk)),
            metadata=stamp,
        )


def iss_party_is_ready(fiscal: FiscalDocument) -> bool:
    """Check that the rendered head is current and has a matching receipt."""
    if current_tenant_id.get() != fiscal.tenant_id or not _can_have_label(fiscal):
        return False
    with connection.cursor() as cursor:
        cursor.execute(
            _CURRENT_READINESS,
            {
                "fiscal": fiscal.pk,
                "tenant": fiscal.tenant_id,
                "client": fiscal.client_id,
                "revision": fiscal.current_revision_id,
                "version": fiscal.version,
                "family": fiscal.family,
                "state": fiscal.fiscal_state,
                "party": fiscal.iss_retention_party,
                "action": _READY,
                "clean_states": sorted(CLEAN_STATES),
                "span": ISS_SOURCE_SPAN,
                "incoming": None,
                "code": None,
                "after": None,
                "limit": 0,
            },
        )
        result = cursor.fetchone()
    if result is None:
        return False
    witnesses, marks = result
    originals = (
        ISSOriginal(UUID(document_id), key, digest, size, code)
        for document_id, key, digest, size, code in json.loads(witnesses or "[]")
    )
    stamp = _stamp(fiscal, originals)
    return any(json.loads(mark) == stamp for mark in marks)
