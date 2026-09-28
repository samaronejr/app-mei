"""`intake_upload` (S7.5): one client file offered against one request, INSERT-only.

Runs inside a portal request, as `app_portal`, confined to one client by
`PortalMiddleware`. Every row it writes is an INSERT the portal is granted
(`obligations_document`, `cycles_evidencelink`, `cycles_documentintake`,
`cycles_cycleevent`), each checked by the RESTRICTIVE `WITH CHECK` on `client_id`, and
the request and document foreign keys are client-paired composites -- so a request id
belonging to another client is invisible to the lookup (404) and could not be linked
even if it were not.

Attribution comes from the caller's verified context (`client`, `tenant`, `actor`
resolved by the middleware from the membership), never from request data or the file
name (`apps/portal/views.py::document_upload` carries the same rule).

Deduplication is per client and content hash (S5.4 M5): the same bytes twice reuse
the one `Document` and add a second `EvidenceLink` (`relinked=True`) -- no error. The
same bytes against the same request again change nothing and say so.

Interim until T12: a new file is stored with the vault's existing semantics and gets
an intake row in state `recebido`; the portal re-serves it only to its uploader (the
firm sees it from the firm side). T12 adds quarantine, type sniffing and scanning.
"""

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
from django.http import Http404

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.cycles.evidence import (
    CLIENT_OPEN_STATES,
    DocumentIntake,
    EvidenceLink,
    EvidenceRequest,
    IntakeState,
    LinkOrigin,
)
from apps.cycles.records import CycleEvent
from apps.obligations.models import Document
from apps.tenants.models import Tenant

CHUNK_BYTES: Final = 64 * 1024
SHA256_CONSTRAINT: Final = "document_tenant_client_sha256_uniq"
LINK_CONSTRAINT: Final = "evidencelink_request_document_uniq"


def _constraint(error: IntegrityError) -> str | None:
    cause = error.__cause__
    diag = getattr(cause, "diag", None)
    return getattr(diag, "constraint_name", None)


class UploadTooLarge(Exception):  # noqa: N818 - domain vocabulary
    """The file exceeds the portal upload cap; nothing was read or written."""


class UploadMissing(Exception):  # noqa: N818 - domain vocabulary
    """No file arrived with the request."""


@dataclass(frozen=True, slots=True)
class IntakeResult:
    """What happened, for the page to say it: new file, relink, or already sent."""

    link: EvidenceLink
    document: Document
    relinked: bool
    already_linked: bool


def _read_capped(upload: UploadedFile) -> tuple[bytes, str]:
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


def _store(  # noqa: PLR0913 - keyword-only: every value is verified context
    *,
    tenant: Tenant,
    client: ClientCompany,
    actor: User,
    upload: UploadedFile,
    payload: bytes,
    digest: str,
) -> Document:
    document = Document(
        tenant=tenant,
        client=client,
        sha256=digest,
        original_filename=(upload.name or "documento")[:255],
        content_type=(upload.content_type or "application/octet-stream")[:127],
        byte_size=len(payload),
        uploaded_by=actor,
    )
    saved = default_storage.save(document.storage_key, ContentFile(payload))
    if saved != document.storage_key:
        _discard(saved)
        msg = "storage changed the requested document key"
        raise RuntimeError(msg)
    try:
        with transaction.atomic():
            document.save(force_insert=True)
            DocumentIntake.objects.create(
                tenant=tenant,
                client=client,
                document=document,
                state=IntakeState.RECEIVED,
            )
    except Exception:
        _discard(saved)
        raise
    return document


def _discard(name: str) -> None:
    try:
        default_storage.delete(name)  # PILOT-205-COMPENSATING-DELETE
    except Exception as error:  # noqa: BLE001 - never mask the root failure
        sentry_sdk.capture_exception(error)


def intake_upload(
    actor: User,
    request_id: UUID,
    upload: UploadedFile | None,
    *,
    tenant: Tenant,
    client: ClientCompany,
) -> IntakeResult:
    """Store (or reuse) the file and link it to the client's open request."""
    request = EvidenceRequest.objects.filter(
        pk=request_id, state__in=CLIENT_OPEN_STATES
    ).first()
    if request is None:
        raise Http404
    if upload is None:
        raise UploadMissing
    payload, digest = _read_capped(upload)
    document = Document.objects.filter(sha256=digest).first()
    relinked = document is not None
    if document is None:
        try:
            document = _store(
                tenant=tenant,
                client=client,
                actor=actor,
                upload=upload,
                payload=payload,
                digest=digest,
            )
        except IntegrityError as error:
            if _constraint(error) != SHA256_CONSTRAINT:
                raise
            # The same bytes stored by a concurrent upload of this client: reuse it.
            document = Document.objects.get(sha256=digest)
            relinked = True
    link = EvidenceLink.objects.filter(request=request, document=document).first()
    if link is not None:
        return IntakeResult(link, document, relinked=True, already_linked=True)
    try:
        with transaction.atomic():
            link = EvidenceLink.objects.create(
                tenant=tenant,
                client=client,
                request=request,
                document=document,
                origin=LinkOrigin.PORTAL,
                relinked=relinked,
                submitted_by=actor,
            )
    except IntegrityError as error:
        if _constraint(error) != LINK_CONSTRAINT:
            raise
        # A concurrent submission of the same bytes won the unique key; that is the
        # same outcome as "already sent", not an error.
        link = EvidenceLink.objects.get(request=request, document=document)
        return IntakeResult(link, document, relinked=True, already_linked=True)
    CycleEvent.objects.create(
        tenant=tenant,
        client=client,
        cycle_id=request.cycle_id,
        actor=actor,
        type="evidence.submitted",
        aggregate_type="EvidenceRequest",
        aggregate_id=request.pk,
        aggregate_version=request.version,
        payload={
            "link_id": str(link.pk),
            "document_id": str(document.pk),
            "sha256": digest,
            "relinked": relinked,
        },
    )
    return IntakeResult(link, document, relinked=relinked, already_linked=False)
