"""Quarantine, scan, extract and register originals without client submissions.

Vault and scanner I/O precede the register transaction. The client row lock
serializes identity creation and head movement. Stale snapshots stay immutable.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Final
from uuid import UUID
from xml.etree.ElementTree import Element, SubElement, tostring

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.services import ObjectRef, record_event
from apps.authz.objects import load_visible_client
from apps.authz.services import can
from apps.clients.models import ClientCompany
from apps.core.flags import tenant_has
from apps.core.retention import RetentionClass
from apps.core.tasks import assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.cycles.evidence import (
    CLEAN_STATES,
    DocumentIntake,
    ExtractedField,
    FieldOrigin,
    IntakeState,
)
from apps.cycles.extraction.v2 import (
    EXTRACTOR_VERSION,
    MAX_BYTES,
    NFE,
    FiscalExtraction,
    extract,
)
from apps.cycles.intake import staff_upload, vault_upload
from apps.cycles.parsing import UnsafeDocument
from apps.cycles.scanning import scan_intake, scanner_from_settings
from apps.integrations.authority import Problem, raise_problem
from apps.integrations.models import ClientProviderLink, ProviderEnvironment
from apps.obligations.models import (
    AcquisitionOrigin,
    Document,
    FiscalDocument,
    FiscalRevision,
    RevisionArtifact,
)
from apps.obligations.system_ingest import ingest_system_document
from apps.tenants.models import Membership, Tenant

ROLE_COUNTERPART: Final = {
    "destinatario": "emitente",
    "emitente": "destinatario",
    "tomador": "prestador",
    "prestador": "tomador",
}
TERMINAL_STATES: Final = ("cancelada", "substituida")
REFERENCE_MAX: Final = 64
KEY_MAX: Final = 128
MUNICIPALITY_CODE_LENGTH: Final = 7
MONEY_MAX: Final = Decimal(10**12)
CENT: Final = Decimal("0.01")
BINDING_ERROR: Final = "Resumo sem vinculo de captura valido para este cliente."


@dataclass(frozen=True, slots=True)
class FiscalSource:
    """Trusted routing and source identity, separate from untrusted XML fields."""

    tenant_id: UUID
    client_id: UUID
    source: str
    provider: str
    provider_version: str
    environment: str
    client_role: str
    source_namespace: str
    receipt_ref: str
    actor: User | None
    capture_binding_id: UUID | None
    fiscal_state: str | None = None
    replacement_key: str = ""
    source_effective_at: datetime | None = None


def _money(raw: str) -> Decimal:
    value = Decimal(raw)
    if (
        not value.is_finite()
        or value < 0
        or value >= MONEY_MAX
        or value != value.quantize(CENT)
    ):
        raise ValueError(raw)
    return value


def _projection(extraction: FiscalExtraction) -> dict[str, object]:
    values = {field.name: field.value for field in extraction.fields}
    projection: dict[str, object] = {}
    try:
        for name in ("total", "iss_stated"):
            projection[name] = _money(values[name]) if name in values else None
        for name in ("issue_date", "competence"):
            projection[name] = (
                date.fromisoformat(values[name]) if name in values else None
            )
    except (InvalidOperation, ValueError) as error:
        message = "Valor ou data fiscal invalida."
        raise ValidationError(message) from error
    municipality = values.get("municipality_incidence", "")
    if municipality and (
        len(municipality) != MUNICIPALITY_CODE_LENGTH
        or not municipality.isascii()
        or not municipality.isdecimal()
    ):
        message = "Municipio de incidencia invalido."
        raise ValidationError(message)
    projection["municipality_incidence"] = municipality
    retained = values.get("iss_withheld_flag", "")
    withheld = {"1": False, "2": True, "3": True}.get(retained)
    projection["iss_withheld_flag"] = withheld
    projection["iss_withheld_amount"] = (
        projection["iss_stated"] if withheld is True else None
    )
    return projection


def _authorize(context: FiscalSource) -> tuple[Tenant, ClientCompany]:
    # PLATFORM_QUERY_OK: trusted ingestion names one exact tenant.
    tenant = Tenant.objects.get(pk=context.tenant_id)
    client = ClientCompany.objects.select_for_update().get(pk=context.client_id)
    if not tenant_has(context.tenant_id, "document_capture"):
        raise PermissionDenied
    if context.actor is not None and context.source == "importacao_equipe":
        load_visible_client(context.actor, client.pk, tenant_id=tenant.pk)
        if not can(context.actor, "cycles.operate", tenant_id=tenant.pk):
            raise PermissionDenied
    if (
        context.source == "upload_cliente"
        # PLATFORM_QUERY_OK: verify the one portal human's membership for this client.
        and not Membership.objects.filter(
            tenant_id=tenant.pk, client=client, user=context.actor, is_active=True
        ).exists()
    ):
        raise PermissionDenied
    return tenant, client


def _binding(context: FiscalSource, client: ClientCompany) -> dict[str, str]:
    if context.capture_binding_id is None:
        raise ValidationError(BINDING_ERROR)
    link = (
        ClientProviderLink.objects.select_for_update()
        .select_related("connection")
        .filter(
            pk=context.capture_binding_id,
            client=client,
            connection__provider=context.provider,
            connection__environment=context.environment,
            connection__status="ativa",
            status="ativa",
            disablement_state="ativa",
            credential_state__in=("ativa", "vencendo"),
        )
        .first()
    )
    if (
        context.source != "captura"
        or context.provider != "focus"
        or link is None
        or (link.certificate_cnpj and link.certificate_cnpj != client.cnpj)
    ):
        raise ValidationError(BINDING_ERROR)
    return {
        "role_basis": "capture_binding",
        "link_id": str(link.pk),
        "provider_company_ref": link.provider_company_ref,
        "client_cnpj": client.cnpj,
    }


def _payload(
    context: FiscalSource,
    payload: bytes | None,
    summary: Mapping[str, str] | None,
) -> bytes:
    invalid = (
        context.source not in ("captura", "importacao_equipe", "upload_cliente")
        or context.environment not in ProviderEnvironment.values
        or context.client_role not in ROLE_COUNTERPART
        or not context.provider_version.isascii()
        or not context.provider_version.isdecimal()
        or len(context.provider_version) > REFERENCE_MAX
        or not context.source_namespace
        or len(context.source_namespace) > REFERENCE_MAX
        or (
            context.source == "captura"
            and (context.actor is not None or not context.receipt_ref)
        )
        or (context.source != "captura" and context.actor is None)
        or (
            context.fiscal_state is not None
            and context.fiscal_state
            not in (
                "autorizada",
                "cancelada",
                "substituida",
                "denegada",
                "desconhecida",
            )
        )
        or len(context.replacement_key) > KEY_MAX
        or (context.replacement_key and context.fiscal_state != "substituida")
        or (
            context.source_effective_at is not None
            and timezone.is_naive(context.source_effective_at)
        )
        or (
            context.source != "captura"
            and (
                context.fiscal_state is not None
                or context.replacement_key
                or context.source_effective_at is not None
            )
        )
        or (payload is None) == (summary is None)
    )
    if invalid:
        message = "Contexto fiscal invalido."
        raise ValidationError(message)
    if summary is not None:
        if context.source != "captura" or context.capture_binding_id is None:
            raise ValidationError(BINDING_ERROR)
        root = Element(f"{NFE}resNFe")
        for name in ("chNFe", "CNPJ", "CPF", "xNome", "dhEmi", "vNF", "cSitNFe"):
            if name in summary:
                SubElement(root, f"{NFE}{name}").text = summary[name]
        payload = tostring(root, encoding="utf-8", xml_declaration=True)
    if payload is None:
        message = "Documento fiscal ausente."
        raise ValidationError(message)
    if len(payload) > MAX_BYTES:
        code = "too_large"
        raise UnsafeDocument(code)
    return payload


def _original(context: FiscalSource, payload: bytes) -> Document:
    with tenant_context(context.tenant_id):
        tenant, client = _authorize(context)
    if context.source == "captura":
        return ingest_system_document(
            tenant_id=tenant.pk,
            client_id=client.pk,
            origin=AcquisitionOrigin.PROVIDER_CAPTURE,
            receipt_ref=context.receipt_ref,
            payload=payload,
            filename="documento-fiscal.xml",
            content_type="application/xml",
            retention_class=RetentionClass.FISCAL_EVIDENCE,
        )
    if context.actor is None:
        raise PermissionDenied
    with tenant_context(tenant.pk):
        upload = SimpleUploadedFile("documento-fiscal.xml", payload)
        document, _ = (
            staff_upload(context.actor, upload, tenant=tenant, client=client)
            if context.source == "importacao_equipe"
            else vault_upload(context.actor, upload, tenant=tenant, client=client)
        )
    return document


def _classify(
    original: Document, intake: DocumentIntake, extraction: FiscalExtraction, role: str
) -> None:
    if not ExtractedField.objects.filter(
        document=original, extractor_version=EXTRACTOR_VERSION
    ).exists():
        ExtractedField.objects.bulk_create(
            [
                ExtractedField(
                    tenant_id=original.tenant_id,
                    client_id=original.client_id,
                    document=original,
                    name=field.name,
                    value=field.value,
                    origin=field.origin,
                    source_span=field.source_span,
                    extractor_version=EXTRACTOR_VERSION,
                )
                for field in extraction.fields
            ]
        )
        if extraction.note:
            ExtractedField.objects.create(
                tenant_id=original.tenant_id,
                client_id=original.client_id,
                document=original,
                name="layout_note",
                value=extraction.note,
                origin=FieldOrigin.XML,
                extractor_version=EXTRACTOR_VERSION,
            )
    state = IntakeState.DIVERGENT if role == "divergent" else IntakeState.CLASSIFIED
    reason = "documento_fiscal_divergente" if role == "divergent" else extraction.note
    intake = DocumentIntake.objects.select_for_update().get(pk=intake.pk)
    if intake.state != state or intake.reason != reason:
        intake.state, intake.reason = state, reason
        intake.version += 1
        intake.save(update_fields=["state", "reason", "version"])


def _incident(
    fiscal: FiscalDocument, client: ClientCompany, original: Document
) -> None:
    fiscal.fiscal_state = "incerta"
    fiscal.version += 1
    fiscal.save(update_fields=["fiscal_state", "version"])
    raise_problem(
        client,
        Problem("incidente_consistencia_fiscal", fiscal.pk, timezone.localdate()),
    )
    record_event(
        action="fiscal.consistency_incident",
        tenant_id=client.tenant_id,
        obj=ObjectRef("FiscalDocument", str(fiscal.pk)),
        metadata={"original_document_id": str(original.pk)},
    )


def _register(
    context: FiscalSource,
    original: Document,
    intake: DocumentIntake,
    extraction: FiscalExtraction,
) -> FiscalDocument:
    projection = _projection(extraction)
    values = {field.name: field.value for field in extraction.fields}
    state = (
        context.fiscal_state
        if context.fiscal_state is not None
        else extraction.fiscal_state
    )
    semantics: dict[str, object] = {
        "family": extraction.family,
        "key": extraction.document_key,
        "state": state,
        "fields": values,
        "summary_only": extraction.summary_only,
        "note": extraction.note,
    }
    if context.replacement_key or context.source_effective_at is not None:
        semantics.update(
            replacement_key=context.replacement_key,
            source_effective_at=(
                context.source_effective_at.astimezone(UTC).isoformat()
                if context.source_effective_at is not None
                else None
            ),
        )
    digest = hashlib.sha256(
        json.dumps(
            semantics,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    _, client = _authorize(context)
    provenance = (
        _binding(context, client) if extraction.summary_only else {"role_basis": "xml"}
    )
    if context.fiscal_state is not None:
        provenance["status_basis"] = "provider_snapshot"
    fiscal, _ = FiscalDocument.objects.get_or_create(
        client=client,
        environment=context.environment,
        family=extraction.family,
        key_namespace=extraction.key_namespace,
        document_key=extraction.document_key,
        defaults={
            "tenant_id": context.tenant_id,
            "client_role": context.client_role,
            "source": context.source,
            "provider": context.provider,
        },
    )
    fiscal = (
        FiscalDocument.objects.select_for_update(of=("self",))
        .select_related("current_revision")
        .get(pk=fiscal.pk)
    )
    role = (
        "unconfirmed"
        if extraction.summary_only or extraction.note
        else (
            "confirmed"
            if values.get(f"{context.client_role}_id") == client.cnpj
            else "divergent"
        )
    )
    _classify(original, intake, extraction, role)
    if role == "divergent":
        raise_problem(
            client,
            Problem("documento_fiscal_divergente", fiscal.pk, timezone.localdate()),
        )
    revision = FiscalRevision.objects.filter(
        document=fiscal,
        source_namespace=context.source_namespace,
        provider_version=context.provider_version,
    ).first()
    if revision is not None and revision.semantic_digest != digest:
        _incident(fiscal, client, original)
        return fiscal
    if revision is None:
        revision = FiscalRevision.objects.create(
            tenant_id=context.tenant_id,
            client=client,
            document=fiscal,
            source_namespace=context.source_namespace,
            provider_version=context.provider_version,
            semantic_digest=digest,
            fiscal_state=state,
            source_effective_at=context.source_effective_at,
            replacement_key=context.replacement_key,
            receipt_ref=context.receipt_ref,
            provenance=provenance,
        )
        record_event(
            action="fiscal.revision_registered",
            tenant_id=context.tenant_id,
            actor=context.actor,
            obj=ObjectRef("FiscalRevision", str(revision.pk)),
            metadata={"document_id": str(fiscal.pk), "role_verification": role},
        )
    RevisionArtifact.objects.get_or_create(
        client=client,
        revision=revision,
        document=original,
        role="resumo" if extraction.summary_only else "xml_original",
        defaults={"tenant_id": context.tenant_id},
    )
    newest_source_version = max(
        int(value)
        for value in FiscalRevision.objects.filter(
            document=fiscal, source_namespace=context.source_namespace
        ).values_list("provider_version", flat=True)
    )
    if int(context.provider_version) < newest_source_version:
        return fiscal
    if fiscal.current_revision is not None and (
        fiscal.current_revision.fiscal_state in TERMINAL_STATES
        and state != fiscal.current_revision.fiscal_state
    ):
        _incident(fiscal, client, original)
        return fiscal
    if fiscal.current_revision_id == revision.pk:
        return fiscal
    counterpart = ROLE_COUNTERPART[context.client_role]
    for name, value in projection.items():
        setattr(fiscal, name, value)
    fiscal.counterpart_id = values.get(f"{counterpart}_id", "")
    fiscal.counterpart_name = values.get(f"{counterpart}_name", "")
    fiscal.current_revision = revision
    fiscal.fiscal_state = state
    fiscal.role_verification = role
    fiscal.summary_only = extraction.summary_only
    fiscal.version += 1
    fiscal.save()
    record_event(
        action="fiscal.head_changed",
        tenant_id=context.tenant_id,
        actor=context.actor,
        obj=ObjectRef("FiscalRevision", str(revision.pk)),
        metadata={"document_id": str(fiscal.pk), "role_verification": role},
    )
    return fiscal


def ingest_fiscal_document(  # noqa: PLR0913 - the plan's keyword-only server contract
    *,
    tenant_id: UUID,
    client_id: UUID,
    source: str,
    provider: str,
    provider_version: str,
    payload_bytes: bytes | None,
    summary: Mapping[str, str] | None,
    actor: User | None,
    environment: str,
    client_role: str,
    source_namespace: str,
    receipt_ref: str,
    capture_binding_id: UUID | None = None,
    fiscal_state: str | None = None,
    replacement_key: str = "",
    source_effective_at: datetime | None = None,
) -> FiscalDocument | None:
    """Return its fiscal identity, or None while the scan cannot clear the original."""
    assert_no_transaction()
    context = FiscalSource(
        tenant_id,
        client_id,
        source,
        provider,
        provider_version,
        environment,
        client_role,
        source_namespace,
        receipt_ref,
        actor,
        capture_binding_id,
        fiscal_state,
        replacement_key,
        source_effective_at,
    )
    payload = _payload(context, payload_bytes, summary)
    with tenant_context(tenant_id):
        _, client = _authorize(context)
        if summary is not None:
            _binding(context, client)
    original = _original(context, payload)
    with tenant_context(tenant_id):
        intake = DocumentIntake.objects.get(document=original)
    if intake.state not in CLEAN_STATES:
        scan_intake(
            tenant_id, intake.pk, scanner=scanner_from_settings(), now=timezone.now()
        )
        with tenant_context(tenant_id):
            intake = DocumentIntake.objects.get(pk=intake.pk)
    if intake.state not in CLEAN_STATES:
        return None
    extraction = extract(intake, payload)
    if not extraction.document_key or len(extraction.document_key) > KEY_MAX:
        code = "missing_fiscal_key"
        raise UnsafeDocument(code)
    with tenant_context(tenant_id):
        return _register(context, original, intake, extraction)
