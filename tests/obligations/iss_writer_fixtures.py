"""National receipt inputs and old-writer event shapes for live ISS scenarios."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING
from unittest.mock import patch

from botocore.exceptions import BotoCoreError, ClientError
from s3transfer.exceptions import (
    RetriesExceededError,
    S3DownloadFailedError,
    S3ValidationError,
)

from apps.audit.services import record_event
from apps.obligations import fiscal_ingest
from apps.obligations import iss_retention_evidence_v1 as evidence
from tests.obligations.iss_vault_fixture import fault_iss_original

if TYPE_CHECKING:
    from uuid import UUID

    from apps.accounts.models import User
    from apps.audit.models import Event
    from apps.audit.services import ObjectRef


@contextmanager
def no_vault_escape() -> Iterator[None]:
    try:
        yield
    except (
        OSError,
        BotoCoreError,
        ClientError,
        RetriesExceededError,
        S3DownloadFailedError,
        S3ValidationError,
    ) as error:
        msg = f"Retained evidence escaped as {type(error).__name__}"
        raise AssertionError(msg) from error


@contextmanager
def post_scan_503(payload: bytes) -> Iterator[None]:
    def unavailable(witness: evidence.ISSOriginal) -> evidence.ISSDecision:
        assert witness.storage_key is not None
        with fault_iss_original("503", witness.storage_key, payload):
            return evidence.verify_iss_original(witness)

    with patch.object(fiscal_ingest, "verify_iss_original", side_effect=unavailable):
        yield


@contextmanager
def old_writer_events() -> Iterator[None]:
    def emit(
        *,
        action: str,
        tenant_id: "UUID",
        obj: "ObjectRef",
        metadata: dict[str, object],
        actor: "User | None" = None,
    ) -> "Event":
        if action == "fiscal.consistency_incident":
            metadata = {"original_document_id": metadata["original_document_id"]}
        elif action == "fiscal.head_changed":
            metadata = {
                k: v for k, v in metadata.items() if k != "iss_projection_owned"
            }
        return record_event(
            action=action, tenant_id=tenant_id, actor=actor, obj=obj, metadata=metadata
        )

    with patch.object(fiscal_ingest, "record_event", side_effect=emit):
        yield
