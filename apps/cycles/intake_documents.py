"""Shared capped, quarantined document storage for human and actor-less intake."""

import hashlib
from dataclasses import dataclass
from typing import Final
from uuid import UUID

import sentry_sdk
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import UploadedFile
from django.db import IntegrityError, transaction

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.retention import RetentionClass
from apps.cycles.evidence import (
    REJECTED_STATES,
    DocumentIntake,
    IntakeState,
    quarantine_key,
)
from apps.cycles.sniffing import sniff
from apps.obligations.models import AcquisitionOrigin, Document
from apps.tenants.models import Tenant

CHUNK_BYTES: Final = 64 * 1024
SHA256_CONSTRAINT: Final = "document_tenant_client_sha256_uniq"


class UploadTooLarge(Exception):  # noqa: N818 - existing intake vocabulary
    """The file exceeds the shared upload cap; nothing was stored."""


class UploadMissing(Exception):  # noqa: N818 - existing intake vocabulary
    """No bytes arrived."""


class UploadRejected(Exception):  # noqa: N818 - existing intake vocabulary
    """The original was retained but refused, or matches a previously refused file."""


@dataclass(frozen=True, slots=True)
class DocumentSource:
    """Attribution and visibility fixed by the trusted intake entry point."""

    actor: User | None
    portal_visible: bool = True
    client_upload: bool = True
    origin: str = AcquisitionOrigin.UPLOAD_PORTAL
    receipt_ref: str = ""
    retention_class: str = RetentionClass.FISCAL_EVIDENCE


def constraint_name(error: IntegrityError) -> str | None:
    """Read psycopg's named constraint without relying on translated error text."""
    cause = error.__cause__
    diag = getattr(cause, "diag", None)
    return getattr(diag, "constraint_name", None)


def read_capped(upload: UploadedFile) -> tuple[bytes, str]:
    """Hash while reading, refusing past the cap before buffering more than it."""
    cap = settings.PORTAL_UPLOAD_MAX_BYTES
    if upload.size is None or upload.size > cap:
        raise UploadTooLarge
    digest = hashlib.sha256()
    parts: list[bytes] = []
    total = 0
    for chunk in upload.chunks(CHUNK_BYTES):
        total += len(chunk)
        if total > cap:
            raise UploadTooLarge
        digest.update(chunk)
        parts.append(chunk)
    if total == 0:
        raise UploadMissing
    return b"".join(parts), digest.hexdigest()


def _store(  # noqa: PLR0913 - verified context, bytes and their source
    *,
    tenant: Tenant,
    client: ClientCompany,
    source: DocumentSource,
    upload: UploadedFile,
    payload: bytes,
    digest: str,
    obligation_id: UUID | str | None,
) -> tuple[Document, DocumentIntake]:
    sniffed = sniff(payload)
    document = Document(
        tenant=tenant,
        client=client,
        obligation_id=obligation_id,
        sha256=digest,
        original_filename=(upload.name or "documento")[:255],
        content_type=sniffed or "application/octet-stream",
        byte_size=len(payload),
        uploaded_by=source.actor,
        portal_visible=source.portal_visible,
        client_upload=source.client_upload,
        acquisition_origin=source.origin,
        acquisition_receipt_ref=source.receipt_ref,
        retention_class=source.retention_class,
    )
    held = quarantine_key(tenant.pk, client.pk, document.storage_key)
    saved = default_storage.save(held, ContentFile(payload))
    if saved != held:
        _discard(saved)
        msg = "storage changed the requested quarantine key"
        raise RuntimeError(msg)
    try:
        with transaction.atomic():
            document.save(force_insert=True)
            intake = DocumentIntake.objects.create(
                tenant=tenant,
                client=client,
                document=document,
                state=IntakeState.QUARANTINED if sniffed else IntakeState.REJECTED_TYPE,
                reason="" if sniffed else "tipo_nao_aceito",
                sniffed_type=sniffed or "",
                quarantine_key=held,
            )
    except Exception:
        # Compensate the storage write on ANY database failure, then propagate it.
        _discard(saved)
        raise
    return document, intake


def _discard(name: str) -> None:
    try:
        default_storage.delete(name)  # PILOT-205-COMPENSATING-DELETE
    except Exception as error:  # noqa: BLE001 - never mask the root failure
        sentry_sdk.capture_exception(error)


def document_for(  # noqa: PLR0913 - same context as the existing intake contract
    *,
    tenant: Tenant,
    client: ClientCompany,
    source: DocumentSource,
    upload: UploadedFile,
    payload: bytes,
    digest: str,
    obligation_id: UUID | str | None = None,
) -> tuple[Document, bool]:
    """Reuse bytes only within one client AND visibility partition."""
    documents = Document.objects.filter(
        client=client, portal_visible=source.portal_visible
    )
    document = documents.filter(sha256=digest).first()
    if document is None:
        try:
            document, intake = _store(
                tenant=tenant,
                client=client,
                source=source,
                upload=upload,
                payload=payload,
                digest=digest,
                obligation_id=obligation_id,
            )
        except IntegrityError as error:
            if constraint_name(error) != SHA256_CONSTRAINT:
                raise
            document = documents.get(sha256=digest)
        else:
            if intake.state == IntakeState.REJECTED_TYPE:
                raise UploadRejected
            return document, False
    if document.purged_at is not None:
        raise UploadRejected
    if DocumentIntake.objects.filter(
        document=document, state__in=REJECTED_STATES
    ).exists():
        raise UploadRejected
    return document, True
