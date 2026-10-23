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

Required scanning (AXRM-005): for a firm carrying `scan_required`, both entry points
refuse with `UploadScannerOff` while `CLAMAV_ENABLED` is off -- before the file is
read, so no bytes, `Document`, intake or link exist. Without the flag the R1 fallback
stands: the file is stored and later becomes `verificacao_indisponivel`.

AXRM-012: `staff_upload` quarantines firm-side evidence under its human uploader,
portal-private by default, and `apps.obligations.system_ingest` builds actor-less
provider originals from the same primitives. Reuse never crosses a client or a
visibility boundary.
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
from apps.authz.objects import load_visible_client
from apps.clients.models import ClientCompany
from apps.core.retention import RetentionClass
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
from apps.cycles.scanning import scan_required
from apps.cycles.sniffing import sniff
from apps.obligations.models import AcquisitionOrigin, Document
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


class UploadScannerOff(Exception):  # noqa: N818 - domain vocabulary
    """The firm requires the integrated scanner and it is switched off; nothing kept."""


def require_scanner(tenant: Tenant) -> None:
    """Refuse before reading any bytes when the firm requires a scanner that is off."""
    if not settings.CLAMAV_ENABLED and scan_required(tenant.pk):
        raise UploadScannerOff


@dataclass(frozen=True, slots=True)
class IntakeResult:
    """What happened, for the page to say it: new file, relink, or already sent."""

    link: EvidenceLink
    document: Document
    relinked: bool
    already_linked: bool


@dataclass(frozen=True, slots=True)
class DocumentSource:
    """Attribution and portal visibility, fixed by the trusted entry point (AXRM-012).

    The defaults are a client's portal upload, exactly as before AXRM-012. Firm-only
    staff evidence and actor-less provider originals pass `portal_visible=False`, so
    the restrictive portal policy hides them even after a clean scan.
    """

    actor: User | None
    portal_visible: bool = True
    client_upload: bool = True
    origin: str = AcquisitionOrigin.UPLOAD_PORTAL
    receipt_ref: str = ""
    retention_class: str = RetentionClass.FISCAL_EVIDENCE


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


def _store(  # noqa: PLR0913 - keyword-only: every value is verified context
    *,
    tenant: Tenant,
    client: ClientCompany,
    source: DocumentSource,
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
        _discard(saved)
        raise
    return document, intake


def _discard(name: str) -> None:
    try:
        default_storage.delete(name)  # PILOT-205-COMPENSATING-DELETE
    except Exception as error:  # noqa: BLE001 - never mask the root failure
        sentry_sdk.capture_exception(error)


def document_for(  # noqa: PLR0913 - keyword-only: every value is verified context
    *,
    tenant: Tenant,
    client: ClientCompany,
    source: DocumentSource,
    upload: UploadedFile,
    payload: bytes,
    digest: str,
    obligation_id: UUID | str | None = None,
) -> tuple[Document, bool]:
    """Return (document, relinked), or raise `UploadRejected` for refused bytes.

    Bytes are reused only inside one client AND one visibility (AXRM-012): a client
    upload of bytes the firm or a provider already holds privately is a separate,
    visible row, never the hidden one.
    """
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
            if _constraint(error) != SHA256_CONSTRAINT:
                raise
            # The same bytes stored by a concurrent upload of this client: reuse it.
            document = documents.get(sha256=digest)
        else:
            if intake.state == IntakeState.REJECTED_TYPE:
                # Committed with the caller's transaction: the refused original and
                # its intake are the record. No link, no submission event.
                raise UploadRejected
            return document, False
    if document.purged_at is not None:
        raise UploadRejected
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
    require_scanner(tenant)
    if upload is None:
        raise UploadMissing
    payload, digest = read_capped(upload)
    document, relinked = document_for(
        tenant=tenant,
        client=client,
        source=DocumentSource(actor=actor),
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
        if _constraint(error) == "evidencelink_retained_original":
            raise UploadRejected from error
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
    require_scanner(tenant)
    if upload is None:
        raise UploadMissing
    payload, digest = read_capped(upload)
    return document_for(
        tenant=tenant,
        client=client,
        source=DocumentSource(actor=actor),
        upload=upload,
        payload=payload,
        digest=digest,
        obligation_id=obligation_id,
    )


def staff_upload(  # noqa: PLR0913 - human context plus the two AXRM-012 source keywords
    actor: User,
    upload: UploadedFile | None,
    *,
    tenant: Tenant,
    client: ClientCompany,
    portal_visible: bool = False,
    acquisition_origin: str = AcquisitionOrigin.UPLOAD_STAFF,
) -> tuple[Document, bool]:
    """Quarantine firm-side evidence under its human uploader, never as a submission.

    Portfolio-scoped: a client outside the actor's portfolio is a 404 before anything
    is read or stored. Private by default (AXRM-012): firm-only evidence reaches the
    client only through an approved publication, so a caller must opt in to
    `portal_visible=True`. Only the staff origin is accepted here; portal uploads and
    provider acquisitions have their own entry points. The caller checks the
    capability its own action requires.
    """
    visible_client = load_visible_client(actor, client.pk, tenant_id=tenant.pk)
    require_scanner(tenant)
    if upload is None:
        raise UploadMissing
    if acquisition_origin != AcquisitionOrigin.UPLOAD_STAFF:
        raise UploadRejected
    payload, digest = read_capped(upload)
    return document_for(
        tenant=tenant,
        client=visible_client,
        source=DocumentSource(
            actor=actor,
            portal_visible=portal_visible,
            client_upload=False,
            origin=acquisition_origin,
        ),
        upload=upload,
        payload=payload,
        digest=digest,
    )
