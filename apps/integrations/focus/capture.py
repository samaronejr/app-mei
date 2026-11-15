"""Hourly bounded capture: prepare outside transactions, commit a whole page once."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import F, Q
from django.utils import timezone
from redis.exceptions import RedisError

from apps.clients.models import ClientCompany, ClientStatus
from apps.core.flags import tenant_has
from apps.core.retention import RetentionClass
from apps.core.tasks import PlatformTask, assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.cycles.evidence import CLEAN_STATES, DocumentIntake
from apps.cycles.parsing import UnsafeDocument, parse_xml
from apps.cycles.scanning import scan_intake
from apps.cycles.services.exceptions import ExceptionReason
from apps.delivery.models import AutomationPause
from apps.integrations.authority import Problem, authority_for, raise_problem
from apps.integrations.call_contracts import AdmissionDeniedError, StaleClaimError
from apps.integrations.focus.capture_lock import (
    CaptureLockLostError,
    TokenGuard,
    token_guard,
)
from apps.integrations.focus.capture_wire import (
    CaptureReadError,
    CaptureReadLimitedError,
    company_secret_ref,
    company_token,
    digest,
    read_response,
)
from apps.integrations.focus.company import capture_links
from apps.integrations.models import ClientProviderLink, ProviderConnection
from apps.integrations.sync_models import (
    ACTIVE_RUN_STATES,
    CaptureAcquisition,
    SyncRun,
    SyncStream,
)
from apps.integrations.transport_types import ProviderRequest
from apps.obligations import fiscal_ingest
from apps.obligations.models import (
    AcquisitionOrigin,
    Document,
    FiscalRevision,
    RevisionArtifact,
)
from apps.obligations.system_ingest import ingest_system_document

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from apps.integrations.call_models import ProviderCall

PAGE_SIZE: Final = 100
CLIENT_LIMIT: Final = 10
RUN_LEASE: Final = timedelta(minutes=15)
VERSION_MAX_LENGTH: Final = 64
KEY_MIN_LENGTH: Final = 44
KEY_MAX_LENGTH: Final = 50
CNPJ_LENGTH: Final = 14
FAILURE_LIMIT: Final = 3
FAILURE_REASON: Final = "falha_na_captura"
REASON: Final = ExceptionReason(
    "A captura falhou em três execuções seguidas. Confira o provedor e as "
    "autorizações; a próxima execução retoma o cursor confirmado.",
    label="falha na captura de documentos fiscais",
)
_STATES: Final = {
    "autorizada": "autorizada",
    "autorizado": "autorizada",
    "cancelada": "cancelada",
    "cancelado": "cancelada",
    "substituida": "substituida",
    "substituido": "substituida",
    "denegada": "denegada",
}

__all__ = [
    "company_secret_ref",
    "company_token",
    "ensure_stream",
    "sweep",
    "synchronize",
]


class PageRefusedError(Exception):
    """A changed binding, owner or cursor invalidated this prepared page."""


@dataclass(frozen=True, slots=True)
class Entry:
    """Identified provider metadata; client routing never comes from these fields."""

    key: str
    version: str
    emitted_on: date
    state: str
    summary: dict[str, str] | None
    correction: int
    replacement: str
    effective_at: datetime | None


@dataclass(frozen=True, slots=True)
class PreparedEntry:
    """A scanned original plus the XML work registered with its exact revision."""

    fiscal: fiscal_ingest.PreparedFiscalDocument
    call_id: UUID
    correction: Document | None = None
    correction_call_id: UUID | None = None
    correction_sequence: int = 0


@dataclass(frozen=True, slots=True)
class PreparedPage:
    """Every item is prepared or explicitly excluded by the immutable start month."""

    entries: tuple[PreparedEntry, ...]
    receipt_ref: str
    maximum: int
    count: int
    skipped: int
    cursor_version: int


def ensure_stream(
    link: ClientProviderLink,
    collection: str,
    *,
    epoch: int = 1,
    filters: dict[str, str] | None = None,
) -> SyncStream:
    """Only authoritative company/complete-projection filters identify a stream."""
    with tenant_context(link.tenant_id):
        canonical = {"cnpj": link.client.cnpj}
        if collection == "nfsen_recebidas":
            canonical["completa"] = "1"
        if (
            collection not in ("nfe_recebidas", "nfsen_recebidas")
            or type(epoch) is not int
            or epoch < 1
            or any(
                canonical.get(name) != value for name, value in (filters or {}).items()
            )
        ):
            message = "Filtro ou época de captura inválidos."
            raise ValidationError(message)
        fingerprint = digest(
            json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        )
        stream, _ = SyncStream.objects.get_or_create(
            tenant_id=link.tenant_id,
            client_id=link.client_id,
            link=link,
            collection=collection,
            filter_digest=fingerprint,
            epoch=epoch,
        )
    return stream


def _snapshot(link: ClientProviderLink) -> dict[str, str | int]:
    return {
        "connection": str(link.connection_id),
        "company": link.provider_company_ref,
        "environment": link.connection.environment,
        "secret_ref": link.connection.secret_ref,
        "cnpj": link.client.cnpj,
        "credential_generation": link.credential_generation,
        "start_month": link.capture_start_month.isoformat()
        if link.capture_start_month
        else "",
    }


def _refusal(link: ClientProviderLink, collection: str) -> str:
    if not tenant_has(link.tenant_id, "document_capture"):
        return "funcionalidade_desligada"
    if settings.PROVIDERS_PAUSED or settings.FOCUS_PAUSED:
        return "pausado"
    if AutomationPause.objects.filter(tenant_id=link.tenant_id, paused=True).exists():
        return "pausado"
    if (
        link.connection.environment != settings.FOCUS_ENVIRONMENT
        or link.connection.provider != "focus"
        or link.connection.status != "ativa"
        or link.status != "ativa"
        or link.disablement_state != "ativa"
        or link.credential_state not in ("ativa", "vencendo")
        or collection not in link.streams
        or link.client.status != ClientStatus.ACTIVE
        or link.capture_start_month is None
        or link.capture_start_month > timezone.localdate().replace(day=1)
    ):
        return "vinculo_ineligivel"
    if (
        link.connection.environment == "producao"
        and settings.PROVIDER_REAL_DATA_APPROVED is not True
    ):
        return "ambiente_nao_aprovado"
    operation = "FC-NFE" if collection == "nfe_recebidas" else "FC-NFS"
    return (
        ""
        if authority_for(link.client, operation, link=link).usable
        else "autoridade_ausente"
    )


def _locked_link(client_id: UUID, link_id: UUID) -> ClientProviderLink:
    """Use the register's client-first lock order for both claim and page commit."""
    client = ClientCompany.objects.select_for_update().get(pk=client_id)
    link = ClientProviderLink.objects.select_for_update().get(
        pk=link_id, client_id=client_id
    )
    link.client = client
    link.connection = ProviderConnection.objects.select_for_update().get(
        pk=link.connection_id
    )
    return link


def _open_run(stream: SyncStream) -> tuple[SyncRun, ClientProviderLink] | None:
    with tenant_context(stream.tenant_id):
        link = _locked_link(stream.client_id, stream.link_id)
        stream = SyncStream.objects.select_for_update().get(pk=stream.pk)
        refusal = _refusal(link, stream.collection)
        if refusal:
            if refusal != "funcionalidade_desligada":
                stream.status, stream.last_error_code = "bloqueada", refusal
                stream.save(update_fields=["status", "last_error_code"])
            return None
        now = timezone.now()
        if stream.latest_attempt_at and now < stream.latest_attempt_at + timedelta(
            hours=1
        ):
            return None
        active = (
            SyncRun.objects.select_for_update()
            .filter(stream=stream, state__in=ACTIVE_RUN_STATES)
            .first()
        )
        if active is not None:
            if active.lease_until is not None and active.lease_until > now:
                return None
            active.state, active.last_error_code, active.finished_at = (
                "falhou",
                "lease_expirada",
                now,
            )
            active.save(update_fields=["state", "last_error_code", "finished_at"])
        occurrence = digest(
            str(stream.pk),
            str(stream.epoch),
            now.astimezone(UTC).strftime("%Y-%m-%dT%H"),
        )
        if SyncRun.objects.filter(stream=stream, occurrence_key=occurrence).exists():
            return None
        stream.latest_attempt_at = now
        stream.version += 1
        stream.save(update_fields=["latest_attempt_at", "version"])
        run = SyncRun.objects.create(
            tenant_id=stream.tenant_id,
            client_id=stream.client_id,
            stream=stream,
            occurrence_key=occurrence,
            state="executando",
            start_cursor=stream.committed_cursor,
            end_cursor=stream.committed_cursor,
            fence=stream.version,
            lease_owner=uuid4(),
            lease_until=now + RUN_LEASE,
            disable_epoch=link.disable_epoch,
            binding_snapshot=_snapshot(link),
        )
    return run, link


def _version(value: object) -> str:
    if (
        type(value) not in (str, int)
        or not str(value).isascii()
        or not str(value).isdecimal()
        or len(str(value)) > VERSION_MAX_LENGTH
    ):
        raise ValueError
    return str(int(str(value)))


def _entries(payload: bytes, collection: str) -> list[Entry]:
    rows = json.loads(payload)
    if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
        raise ValueError
    entries = []
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError
        key = row.get("chave_nfe" if collection == "nfe_recebidas" else "chave_nfse")
        if (
            not isinstance(key, str)
            or not key.isascii()
            or not key.isdecimal()
            or not KEY_MIN_LENGTH <= len(key) <= KEY_MAX_LENGTH
        ):
            raise ValueError
        version = _version(row.get("versao"))
        state = _STATES[row["situacao"]]
        emitted = datetime.fromisoformat(row["data_emissao"])
        if timezone.is_naive(emitted):
            raise ValueError
        summary = None
        if collection == "nfe_recebidas" and row.get("nfe_completa", False) is not True:
            identity = str(row["documento_emitente"])
            summary = {
                "chNFe": key,
                "CNPJ" if len(identity) == CNPJ_LENGTH else "CPF": identity,
                "xNome": row["nome_emitente"],
                "dhEmi": row["data_emissao"],
                "vNF": str(row["valor_total"]),
                "cSitNFe": {"autorizada": "1", "cancelada": "2", "denegada": "3"}.get(
                    state, "0"
                ),
            }
        correction = int(_version(row.get("numero_carta_correcao") or 0))
        effective = row.get("data_cancelamento")
        effective_at = datetime.fromisoformat(effective) if effective else None
        if effective_at is not None and timezone.is_naive(effective_at):
            raise ValueError
        replacement = row.get("chave_nfse_substituta", "")
        if not isinstance(replacement, str):
            raise TypeError
        if replacement and collection == "nfsen_recebidas":
            replacement = f"NFS{replacement}"
        entries.append(
            Entry(
                key,
                version,
                emitted.date(),
                state,
                summary,
                correction,
                replacement,
                effective_at,
            )
        )
    return entries


def _correction(
    run: SyncRun, link: ClientProviderLink, entry: Entry, guard: TokenGuard
) -> tuple[Document | None, UUID | None, int]:
    if not entry.correction:
        return None, None, 0
    with tenant_context(run.tenant_id):
        retained = CaptureAcquisition.objects.filter(
            stream_id=run.stream_id,
            role="xml_cce",
            revision__document__document_key=entry.key,
            sequence__gte=entry.correction,
        ).exists()
    if retained:
        return None, None, 0
    payload, call, _ = read_response(
        run,
        link,
        ProviderRequest(variant="cce", subject=entry.key),
        identity=digest(
            str(run.stream_id),
            str(run.stream.epoch),
            entry.key,
            entry.version,
            "xml_cce",
            str(entry.correction),
        ),
        guard=guard,
    )
    root = parse_xml(payload)
    sequence = int(root.findtext(".//{*}nSeqEvento", "0"))
    if (
        root.findtext(".//{*}chNFe") != entry.key
        or root.findtext(".//{*}tpEvento") != "110110"
        or sequence < entry.correction
    ):
        raise ValueError
    document = ingest_system_document(
        tenant_id=run.tenant_id,
        client_id=run.client_id,
        origin=AcquisitionOrigin.PROVIDER_CAPTURE,
        receipt_ref=str(call.pk),
        payload=payload,
        filename="carta-correcao.xml",
        content_type="application/xml",
        retention_class=RetentionClass.FISCAL_EVIDENCE,
    )
    with tenant_context(run.tenant_id):
        intake = DocumentIntake.objects.get(document=document)
    if intake.state not in CLEAN_STATES:
        scan_intake(
            run.tenant_id,
            intake.pk,
            scanner=fiscal_ingest.scanner_from_settings(),
            now=timezone.now(),
        )
        with tenant_context(run.tenant_id):
            intake.refresh_from_db()
    if intake.state not in CLEAN_STATES:
        code = "verificacao_indisponivel"
        raise CaptureReadError(code)
    return document, call.pk, sequence


def _prepare_page(
    run: SyncRun,
    link: ClientProviderLink,
    response: tuple[bytes, ProviderCall, str],
    guard: TokenGuard,
) -> PreparedPage:
    """All bytes and scanner I/O finish before any fiscal revision is inserted."""
    payload, call, receipt = response
    entries = _entries(payload, run.stream.collection)
    prepared = []
    skipped = 0
    for entry in entries:
        if (
            link.capture_start_month is not None
            and entry.emitted_on < link.capture_start_month
        ):
            skipped += 1
            continue
        full, original_call_id = None, call.pk
        if entry.summary is None:
            full, original_call, _ = read_response(
                run,
                link,
                ProviderRequest(variant="xml", subject=entry.key),
                identity=digest(
                    str(run.stream_id),
                    str(run.stream.epoch),
                    entry.key,
                    entry.version,
                    "xml_original",
                ),
                guard=guard,
            )
            original_call_id = original_call.pk
        context = fiscal_ingest.FiscalSource(
            tenant_id=run.tenant_id,
            client_id=run.client_id,
            source="captura",
            provider="focus",
            provider_version=entry.version,
            environment=link.connection.environment,
            client_role="destinatario"
            if run.stream.collection == "nfe_recebidas"
            else "tomador",
            source_namespace=f"focus:{run.stream.collection}",
            receipt_ref=str(original_call_id),
            actor=None,
            capture_binding_id=link.pk,
            fiscal_state=entry.state,
            replacement_key=entry.replacement,
            source_effective_at=entry.effective_at,
        )
        fiscal = fiscal_ingest.prepare_fiscal_document(
            context, payload_bytes=full, summary=entry.summary
        )
        if fiscal is None:
            code = "verificacao_indisponivel"
            raise CaptureReadError(code)
        expected_key = (
            entry.key if run.stream.collection == "nfe_recebidas" else f"NFS{entry.key}"
        )
        if fiscal.extraction.document_key != expected_key:
            raise ValueError
        expected_family = (
            "nfe" if run.stream.collection == "nfe_recebidas" else "nfse_nacional"
        )
        if fiscal.extraction.family != expected_family:
            raise ValueError
        correction, correction_call, sequence = _correction(run, link, entry, guard)
        prepared.append(
            PreparedEntry(
                fiscal, original_call_id, correction, correction_call, sequence
            )
        )
    return PreparedPage(
        tuple(prepared),
        receipt,
        max((int(entry.version) for entry in entries), default=int(run.end_cursor)),
        len(entries),
        skipped,
        run.stream.cursor_version,
    )


def _current(run: SyncRun, current: SyncRun) -> bool:
    return (
        current.state == "executando"
        and current.fence == run.fence
        and current.lease_owner == run.lease_owner
        and current.lease_until is not None
        and current.lease_until > timezone.now()
    )


def _commit_page(run: SyncRun, page: PreparedPage, guard: TokenGuard) -> SyncRun:
    """Recheck live admission, register every revision, then compare-and-swap."""
    with tenant_context(run.tenant_id):
        link = _locked_link(run.client_id, run.stream.link_id)
        stream = SyncStream.objects.select_for_update().get(pk=run.stream_id)
        current = SyncRun.objects.select_for_update().get(pk=run.pk)
        guard.require_owned()
        if (
            _refusal(link, stream.collection)
            or link.disable_epoch != run.disable_epoch
            or _snapshot(link) != run.binding_snapshot
            or not _current(run, current)
            or stream.cursor_version != page.cursor_version
        ):
            raise PageRefusedError
        for entry in page.entries:
            prepared = entry.fiscal
            context = prepared.context
            fiscal = prepared.register(page.receipt_ref)
            revision = FiscalRevision.objects.get(
                document=fiscal,
                source_namespace=context.source_namespace,
                provider_version=context.provider_version,
            )
            # A conflicting version was surfaced by the register. Its rejected
            # original must never become an artifact of the established revision.
            if not RevisionArtifact.objects.filter(
                revision=revision, document=prepared.original
            ).exists():
                continue
            retained = not prepared.extraction.summary_only
            CaptureAcquisition.objects.get_or_create(
                tenant_id=run.tenant_id,
                client_id=run.client_id,
                revision=revision,
                role="xml_original",
                sequence=0,
                defaults={
                    "stream": stream,
                    "run": current,
                    "state": "retida" if retained else "pendente",
                    "document": prepared.original if retained else None,
                    "call_id": entry.call_id if retained else None,
                },
            )
            if entry.correction is not None:
                RevisionArtifact.objects.get_or_create(
                    tenant_id=run.tenant_id,
                    client_id=run.client_id,
                    revision=revision,
                    role="xml_cce",
                    document=entry.correction,
                )
                CaptureAcquisition.objects.get_or_create(
                    tenant_id=run.tenant_id,
                    client_id=run.client_id,
                    revision=revision,
                    role="xml_cce",
                    sequence=entry.correction_sequence,
                    defaults={
                        "stream": stream,
                        "run": current,
                        "state": "retida",
                        "document": entry.correction,
                        "call_id": entry.correction_call_id,
                    },
                )
        cursor = str(max(int(stream.committed_cursor), page.maximum))
        changed = SyncStream.objects.filter(
            pk=stream.pk, cursor_version=page.cursor_version
        ).update(
            committed_cursor=cursor,
            cursor_version=F("cursor_version") + 1,
            version=F("version") + 1,
            last_successful_source_observation_at=(
                timezone.now()
                if page.entries
                else stream.last_successful_source_observation_at
            ),
        )
        if changed != 1:
            raise PageRefusedError
        current.end_cursor = cursor
        current.pages_committed += 1
        current.documents_accounted += len(page.entries)
        current.skipped_before_start += page.skipped
        current.lease_until = timezone.now() + RUN_LEASE
        current.save()
        stream.refresh_from_db()
        current.stream = stream
    return current


def _finish(
    run: SyncRun, state: str, code: str = "", *, complete: bool = False
) -> SyncRun:
    with tenant_context(run.tenant_id):
        link = _locked_link(run.client_id, run.stream.link_id)
        stream = SyncStream.objects.select_for_update().get(pk=run.stream_id)
        current = SyncRun.objects.select_for_update().get(pk=run.pk)
        if not _current(run, current):
            return current
        incomplete = (
            CaptureAcquisition.objects.filter(
                stream=stream, revision_id=F("revision__document__current_revision_id")
            )
            .filter(
                Q(state="pendente")
                | ~Q(revision__document__role_verification="confirmed")
                | Q(revision__document__fiscal_state="incerta")
            )
            .exists()
        )
        if state == "concluida" and incomplete:
            state = "parcial"
        current.state, current.last_error_code = state, code
        current.finished_at = timezone.now()
        current.lease_owner = None
        current.lease_until = None
        current.version += 1
        current.save()
        stream.last_error_code = code
        stream.status = (
            "falha_na_captura"
            if state == "falhou"
            else "atualizada"
            if state == "concluida"
            else state
        )
        if state == "falhou":
            stream.consecutive_failures += 1
        elif state != "bloqueada":
            stream.consecutive_failures = 0
        if complete and not incomplete and state == "concluida":
            stream.last_complete_scan_at = timezone.now()
        stream.version += 1
        stream.save()
        if state == "falhou" and stream.consecutive_failures >= FAILURE_LIMIT:
            service = (
                "captura_nfe"
                if stream.collection == "nfe_recebidas"
                else "captura_nfse"
            )
            raise_problem(
                link.client,
                Problem(
                    FAILURE_REASON, stream.pk, timezone.localdate(), service=service
                ),
            )
    return current


def _synchronize_pages(
    run: SyncRun, link: ClientProviderLink, guard: TokenGuard
) -> SyncRun:
    for _ in range(run.page_limit):
        query: tuple[tuple[str, str], ...] = (
            ("cnpj", link.client.cnpj),
            ("versao", run.end_cursor),
        )
        if run.stream.collection == "nfsen_recebidas":
            query += (("completa", "1"),)
        response = read_response(
            run,
            link,
            ProviderRequest(query=query),
            identity=digest(run.occurrence_key, "pagina", run.end_cursor),
            guard=guard,
        )
        with tenant_context(run.tenant_id):
            SyncStream.objects.filter(pk=run.stream_id).update(
                last_api_success_at=timezone.now()
            )
        page = _prepare_page(run, link, response, guard)
        run = _commit_page(run, page, guard)
        if page.count < PAGE_SIZE:
            return _finish(run, "concluida", complete=True)
    return _finish(run, "parcial", "limite_de_paginas")


def synchronize(tenant_id: UUID, stream_id: UUID) -> SyncRun | None:
    """Run at most three pages, outside enclosing transactions, at most hourly."""
    assert_no_transaction()
    with tenant_context(tenant_id):
        stream = SyncStream.objects.select_related(
            "link__connection", "link__client"
        ).get(pk=stream_id)
    token = company_token(stream.link)
    if token is None:
        return None
    with token_guard(token) as guard:
        if guard is None:
            return None
        opened = _open_run(stream)
        if opened is None:
            return None
        run, link = opened
        try:
            return _synchronize_pages(run, link, guard)
        except CaptureReadLimitedError as error:
            state, code = "parcial", error.code
        except (CaptureLockLostError, PageRefusedError, StaleClaimError):
            state, code = "bloqueada", "execucao_substituida"
        except AdmissionDeniedError:
            state, code = "bloqueada", "admissao_recusada"
        except CaptureReadError as error:
            state, code = "falhou", error.code
        except (ValidationError, UnsafeDocument, ValueError, TypeError, KeyError):
            state, code = "falhou", "pagina_invalida"
        except (RedisError, OSError):
            state, code = "falhou", "transporte_indisponivel"
        return _finish(run, state, code)


def _candidates() -> QuerySet[ClientCompany]:
    return ClientCompany.objects.filter(
        pk__in=capture_links().values("client_id"), status=ClientStatus.ACTIVE
    ).order_by("pk")


def sweep() -> int:
    """Visit every enabled firm and at most ten of its clients, then move its cursor."""
    assert_no_transaction()
    if settings.PROVIDERS_PAUSED or settings.FOCUS_PAUSED:
        return 0
    visited = 0
    for tenant_id in PlatformTask.tenant_ids():
        with tenant_context(tenant_id):
            if not tenant_has(tenant_id, "document_capture"):
                continue
            control, _ = AutomationPause.objects.get_or_create(tenant_id=tenant_id)
            if control.paused:
                continue
            ordered = _candidates()
            cursor = control.capture_documents_cursor
            picked = list(
                (ordered.filter(pk__gt=cursor) if cursor else ordered)[:CLIENT_LIMIT]
            )
            if cursor and len(picked) < CLIENT_LIMIT:
                picked += list(
                    ordered.filter(pk__lte=cursor)[: CLIENT_LIMIT - len(picked)]
                )
        for client in picked:
            with tenant_context(tenant_id):
                links = list(
                    capture_links()
                    .filter(client=client)
                    .select_related("client", "connection")
                )
            for link in links:
                for collection in link.streams:
                    stream = ensure_stream(link, collection)
                    synchronize(tenant_id, stream.pk)
            with tenant_context(tenant_id):
                AutomationPause.objects.filter(pk=control.pk).update(
                    capture_documents_cursor=client.pk
                )
            visited += 1
    return visited
