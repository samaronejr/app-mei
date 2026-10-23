"""Trusted worker ingestion: provider originals are actor-less and portal-private."""

from uuid import UUID

from django.core.files.uploadedfile import SimpleUploadedFile

from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.cycles.intake import _require_scanner
from apps.cycles.intake_documents import (
    DocumentSource,
    UploadRejected,
    document_for,
    read_capped,
)
from apps.obligations.models import AcquisitionOrigin, Document
from apps.tenants.models import Tenant


class InvalidSystemDocument(Exception):  # noqa: N818 - domain vocabulary
    """An automated acquisition lacks a provider origin or its opaque receipt."""


def ingest_system_document(  # noqa: PLR0913 - the plan's keyword-only ingest contract
    *,
    tenant_id: UUID,
    client_id: UUID,
    origin: str,
    receipt_ref: str,
    payload: bytes,
    filename: str,
    content_type: str,
    retention_class: str,
) -> Document:
    """Quarantine bytes without inventing a human or reusing a visible upload."""
    if (
        origin
        not in (
            AcquisitionOrigin.PROVIDER_CAPTURE,
            AcquisitionOrigin.FEDERAL_PROVIDER,
        )
        or not receipt_ref
    ):
        raise InvalidSystemDocument
    with tenant_context(tenant_id):
        # PLATFORM_QUERY_OK: the trusted task supplied this exact tenant, not a scan.
        tenant = Tenant.objects.get(pk=tenant_id)
        client = ClientCompany.objects.get(pk=client_id)
        _require_scanner(tenant)
        upload = SimpleUploadedFile(filename, payload, content_type=content_type)
        capped, digest = read_capped(upload)
        try:
            document, _ = document_for(
                tenant=tenant,
                client=client,
                source=DocumentSource(
                    actor=None,
                    portal_visible=False,
                    client_upload=False,
                    origin=origin,
                    receipt_ref=receipt_ref,
                    retention_class=retention_class,
                ),
                upload=upload,
                payload=capped,
                digest=digest,
            )
        except UploadRejected as error:
            # Commit the refused original/intake before reporting the refusal,
            # just as the portal boundary does. Never orphan its held bytes.
            refusal = error
        else:
            return document
    raise refusal
