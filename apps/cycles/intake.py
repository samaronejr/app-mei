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

Quarantine (T12, S8.3): a new file's type is sniffed from its bytes (the declared
type and name are ignored), and the bytes are written under
`quarantine/{tenant}/{client}/` -- never to the final key. An allowed type gets an
intake in `quarentena` and waits for the scan sweep (`apps.cycles.scanning`); a
refused type gets an intake in `rejeitado_tipo`, no link, and the upload is answered
with a refusal. The refused original is retained, never deleted and never served.
Nothing here parses the file. Bytes identical to a refused document are refused
again, without a link.

`vault_upload` (T13) routes the portal vault's free-form upload through the same
sniff -> quarantine -> intake path, with no request and so no link.
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
    REJECTED_STATES,
    DocumentIntake,
    EvidenceLink,
    EvidenceRequest,
    IntakeState,
    LinkOrigin,
    quarantine_key,
)
from apps.cycles.records import CycleEvent
from apps.cycles.sniffing import sniff
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


class UploadRejected(Exception):  # noqa: N818 - domain vocabulary
    """The bytes are not an accepted type (or match a refused document); not linked."""


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
    obligation_id: UUID | str | None = None,
) -> tuple[Document, DocumentIntake]:
    sniffed = sniff(payload)
    document = Document(
        tenant=tenant,
        client=client,
        obligation_id=obligation_id,
        sha256=digest,
        original_filename=(upload.name or "documento")[:255],
        # The sniffed type, never the declared one; a refused file has no type.
        content_type=sniffed or "application/octet-stream",
        byte_size=len(payload),
        uploaded_by=actor,
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
        _discard(saved)
        raise
    return document, intake


def _discard(name: str) -> None:
    try:
        default_storage.delete(name)  # PILOT-205-COMPENSATING-DELETE
    except Exception as error:  # noqa: BLE001 - never mask the root failure
        sentry_sdk.capture_exception(error)


def _document_for(  # noqa: PLR0913 - keyword-only: every value is verified context
    *,
    tenant: Tenant,
    client: ClientCompany,
    actor: User,
    upload: UploadedFile,
    payload: bytes,
    digest: str,
    obligation_id: UUID | str | None = None,
) -> tuple[Document, bool]:
    """Return (document, relinked), or raise `UploadRejected` for refused bytes."""
    document = Document.objects.filter(sha256=digest).first()
    if document is None:
        try:
            document, intake = _store(
                tenant=tenant,
                client=client,
                actor=actor,
                upload=upload,
                payload=payload,
                digest=digest,
                obligation_id=obligation_id,
            )
        except IntegrityError as error:
            if _constraint(error) != SHA256_CONSTRAINT:
                raise
            # The same bytes stored by a concurrent upload of this client: reuse it.
            document = Document.objects.get(sha256=digest)
        else:
            if intake.state == IntakeState.REJECTED_TYPE:
                # Committed with the caller's transaction: the refused original and
                # its intake are the record. No link, no submission event.
                raise UploadRejected
            return document, False
    if DocumentIntake.objects.filter(
        document=document, state__in=REJECTED_STATES
    ).exists():
        raise UploadRejected
    return document, True


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
    document, relinked = _document_for(
        tenant=tenant,
        client=client,
        actor=actor,
        upload=upload,
        payload=payload,
        digest=digest,
    )
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
    _schedule_observation(link)
    return IntakeResult(link, document, relinked=relinked, already_linked=False)


def _schedule_observation(_link: EvidenceLink) -> None:
    """Enqueue the receipt materializer once this upload's transaction commits.

    Only bound `comprovante` requests produce observations, and the portal role
    cannot insert them — so the write is deferred to an `app_runtime` task
    after commit. A broker failure is logged, never swallowed silently: the
    minute sweep is the floor that re-materializes the link regardless. The
    link itself is only the trigger's provenance — the sweep re-finds it.
    """

    def enqueue() -> None:
        # Imported here: the tasks module pulls the generator's import graph,
        # and this file sits at the bottom of it.
        from apps.obligations.tasks import (  # noqa: PLC0415
            materialize_receipt_observations,
        )

        try:
            materialize_receipt_observations.apply_async()
        except Exception as error:  # noqa: BLE001 — enqueue is best-effort; sweep covers
            sentry_sdk.capture_exception(error)

    transaction.on_commit(enqueue)


def vault_upload(
    actor: User,
    upload: UploadedFile | None,
    *,
    tenant: Tenant,
    client: ClientCompany,
    obligation_id: UUID | str | None = None,
) -> tuple[Document, bool]:
    """Store a portal vault upload through the same quarantine as a request upload.

    T13 closes the vault's bypass (plan C-09): the bytes are sniffed, written under the
    quarantine prefix with a `DocumentIntake`, and served to nobody -- not even another
    account of the same client -- until the scanner says clean. Returns (document,
    already_sent); refused bytes raise `UploadRejected` with the refused original and
    its intake kept, exactly as for a request upload.
    """
    if upload is None:
        raise UploadMissing
    payload, digest = _read_capped(upload)
    return _document_for(
        tenant=tenant,
        client=client,
        actor=actor,
        upload=upload,
        payload=payload,
        digest=digest,
        obligation_id=obligation_id,
    )
