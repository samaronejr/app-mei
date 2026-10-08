"""Synthetic divergent uploads and terminal failed sends; never dispatch mail."""

from hashlib import sha256
from typing import Final

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from apps.accounts.models import User
from apps.clients.models import ClientContact
from apps.core.demo_data import ScaleSpec, demo_id
from apps.core.tenancy import tenant_context
from apps.cycles.evidence import FieldOrigin, ScanVerdict
from apps.cycles.models import (
    DocumentIntake,
    EvidenceLink,
    EvidenceRequest,
    ExtractedField,
    IntakeState,
    LinkOrigin,
)
from apps.delivery.models import DeliveryAttempt, MessageState, OutboundMessage
from apps.obligations.models import Document
from apps.tenants.models import Tenant

DIVERGENT_CNPJ: Final = "12ABC34501DE35"


def document_bytes(reference: str) -> bytes:
    """Return a readable XML specimen whose issuer differs from its owning client."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<nota><cnpj>{DIVERGENT_CNPJ}</cnpj><referencia>{reference}</referencia></nota>"
    ).encode()


def seed_evidence(
    requests: list[EvidenceRequest], actor: User, spec: ScaleSpec
) -> None:
    """Populate current-period messy evidence; all other requests remain missing."""
    for index, request in enumerate(requests):
        if index % 4 == 0:
            _divergent_upload(request, actor, spec)
        if index % 10 == 0:
            _failed_send(request, actor, spec)


def _divergent_upload(request: EvidenceRequest, actor: User, spec: ScaleSpec) -> None:
    payload = document_bytes(str(request.pk))
    document = Document.objects.create(
        id=demo_id(spec.seed, f"document:{request.pk}"),
        tenant=request.tenant,
        client=request.client,
        storage_key=f"axorim-demo/{spec.seed}/{request.pk}.xml",
        sha256=sha256(payload).hexdigest(),
        original_filename="nota-sintetica-cnpj-divergente.xml",
        content_type="application/xml",
        byte_size=len(payload),
        uploaded_by=actor,
        client_upload=True,
    )
    DocumentIntake.objects.create(
        id=demo_id(spec.seed, f"intake:{request.pk}"),
        tenant=request.tenant,
        client=request.client,
        document=document,
        state=IntakeState.DIVERGENT,
        reason="cnpj_divergente",
        sniffed_type="application/xml",
        verdict=ScanVerdict.NONE,
        created_at=spec.now,
    )
    EvidenceLink.objects.create(
        id=demo_id(spec.seed, f"link:{request.pk}"),
        tenant=request.tenant,
        client=request.client,
        request=request,
        document=document,
        origin=LinkOrigin.STAFF,
        submitted_by=actor,
        created_at=spec.now,
    )
    ExtractedField.objects.create(
        id=demo_id(spec.seed, f"field:{request.pk}"),
        tenant=request.tenant,
        client=request.client,
        document=document,
        name="cnpj",
        value=DIVERGENT_CNPJ,
        origin=FieldOrigin.XML,
        source_span="/nota/cnpj",
        extractor_version="synthetic-v1",
        created_at=spec.now,
    )


def _failed_send(request: EvidenceRequest, actor: User, spec: ScaleSpec) -> None:
    contact = ClientContact.objects.create(
        id=demo_id(spec.seed, f"contact:{request.pk}"),
        tenant=request.tenant,
        client=request.client,
        name="Contato sintético",
        email=f"cliente-{request.client.pk}@example.com",
        channel_email_allowed=False,
    )
    digest = sha256(str(request.pk).encode()).hexdigest()
    message = OutboundMessage.objects.create(
        id=demo_id(spec.seed, f"message:{request.pk}"),
        tenant=request.tenant,
        client=request.client,
        contact=contact,
        cycle=request.cycle,
        evidence_request=request,
        created_by=actor,
        business_intent_key=digest,
        idempotency_key=digest,
        content_digest=digest,
        recipients_json=[contact.email],
        subject="Aviso sintético de documentos",
        body_ref="synthetic:no-external-effect",
        portal_url=f"http://{request.tenant.slug}-portal.localhost/",
        state=MessageState.REJECTED,
        attempts=1,
        last_error_code="synthetic_rejected",
        next_attempt_at=spec.now,
        created_at=spec.now,
        updated_at=spec.now,
    )
    DeliveryAttempt.objects.create(
        id=demo_id(spec.seed, f"attempt:{request.pk}"),
        tenant=request.tenant,
        client=request.client,
        message=message,
        outcome=MessageState.REJECTED,
        error_code="synthetic_rejected",
        occurred_at=spec.now,
    )


def write_document_files(tenant: Tenant) -> None:
    """Write local bytes after commit; a rerun repairs an interrupted file phase."""
    with tenant_context(tenant.pk):
        documents = list(
            Document.objects.filter(storage_key__startswith="axorim-demo/").values_list(
                "storage_key", "evidence_links__request_id"
            )
        )
    for key, request_id in documents:
        if not default_storage.exists(key):
            default_storage.save(key, ContentFile(document_bytes(str(request_id))))
