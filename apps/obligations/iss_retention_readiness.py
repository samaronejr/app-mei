"""Proof that a scoped document's current ISS label has been projected."""

import hashlib
import json
from collections.abc import Iterable
from typing import Final

from django.db import connection
from django.db.models import QuerySet

from apps.audit.models import Event
from apps.audit.services import ObjectRef, record_event
from apps.obligations.iss_retention_evidence_v1 import (
    ISSDecision,
    ISSOriginal,
    revision_iss_originals,
)
from apps.obligations.models.fiscal import FiscalDocument

_READY: Final = "fiscal.iss_party_ready"


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
    """Check the exact current evidence without contacting the vault or provider."""
    if not _can_have_label(fiscal):
        return False
    originals = revision_iss_originals(
        connection, (fiscal.tenant_id, fiscal.client_id, fiscal.current_revision_id)
    )
    return _marks(fiscal).filter(metadata=_stamp(fiscal, originals)).exists()
