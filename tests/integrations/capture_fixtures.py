"""Synthetic capture graphs and assertion-first imports for AXRM-021."""

from __future__ import annotations

import importlib
import importlib.util
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Literal, TypedDict, Unpack, cast, overload
from unittest.mock import patch
from uuid import uuid4

from django.apps import apps
from django.utils import timezone

from apps.core.models import TenantScopedModel
from apps.core.tenancy import tenant_context
from apps.cycles.scanning import FakeScanner
from apps.integrations.budget_models import ProviderBudget
from apps.integrations.budget_pricing import FOCUS_PLAN
from apps.integrations.call_models import ProviderCall
from apps.integrations.models import (
    AuthorityRecord,
    ClientProviderLink,
    ProviderConnection,
)
from apps.obligations.fiscal_ingest import ingest_fiscal_document
from apps.obligations.models import FiscalDocument
from tests.fakes.provider import Reply
from tests.obligations.fiscal_fixtures import CLIENT, COUNTERPART, KEY, nfe, nfse
from tests.obligations.ingest_fixtures import ingest_setup

if TYPE_CHECKING:
    from datetime import tzinfo
    from pathlib import Path
    from types import ModuleType
    from typing import Self
    from uuid import UUID

    import pytest
    from pytest_django.fixtures import SettingsWrapper

    from apps.cycles.fiscal_records import FiscalDocumentReview
    from apps.integrations.sync_models import CaptureAcquisition, SyncRun, SyncStream
    from tests.portal.request_fixtures import PortalSetup

START = datetime(2026, 9, 20, 12, tzinfo=UTC)
MONTH = date(2026, 9, 1)


class _CaptureDateTime(datetime):
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> Self:
        return cls.fromtimestamp(timezone.now().timestamp(), tz=tz)


def capture_api() -> ModuleType:
    name = "apps.integrations.focus.capture"
    assert importlib.util.find_spec(name) is not None, (
        "AXRM-021 needs the capture synchronizer before a page can be accounted."
    )
    return importlib.import_module(name)


def lock_api() -> ModuleType:
    name = "apps.integrations.focus.capture_lock"
    assert importlib.util.find_spec(name) is not None, (
        "AXRM-021 needs a token guard independent of the page connection."
    )
    return importlib.import_module(name)


def review_api() -> ModuleType:
    name = "apps.cycles.services.fiscal_review"
    assert importlib.util.find_spec(name) is not None, (
        "AXRM-021 needs a revision-pinned fiscal review command."
    )
    return importlib.import_module(name)


@overload
def model(
    name: Literal["SyncStream"], *, app: str = "integrations"
) -> type[SyncStream]: ...


@overload
def model(name: Literal["SyncRun"], *, app: str = "integrations") -> type[SyncRun]: ...


@overload
def model(
    name: Literal["CaptureAcquisition"], *, app: str = "integrations"
) -> type[CaptureAcquisition]: ...


@overload
def model(
    name: Literal["FiscalDocumentReview"], *, app: str = "integrations"
) -> type[FiscalDocumentReview]: ...


@overload
def model(name: str, *, app: str = "integrations") -> type[TenantScopedModel]: ...


def model(name: str, *, app: str = "integrations") -> type[TenantScopedModel]:
    found = apps.all_models[app].get(name.lower())
    assert found is not None, f"AXRM-021 must register {app}.{name}."
    assert issubclass(found, TenantScopedModel)
    return found


def key(index: int) -> str:
    return "33" + f"{index:042d}"


class ItemValues(TypedDict, total=False):
    """Optional received fields used by deterministic page fixtures."""

    state: str
    emitted: str
    cce: int


def item(
    version: int = 1,
    *,
    index: int = 1,
    full: bool = False,
    **values: Unpack[ItemValues],
) -> dict[str, object]:
    correction = values.get("cce", 0)
    return {
        "chave_nfe": key(index),
        "versao": version,
        "nome_emitente": "Synthetic issuer",
        "documento_emitente": COUNTERPART,
        "cnpj_destinatario": CLIENT,
        "valor_total": "1500.25",
        "data_emissao": values.get("emitted", "2026-09-15T12:30:00-03:00"),
        "situacao": values.get("state", "autorizada"),
        "nfe_completa": full,
        "numero_carta_correcao": str(correction) if correction else None,
    }


def page(
    rows: list[dict[str, object]],
    *,
    maximum: int | None = None,
    total: int | None = None,
) -> Reply:
    highest = max((int(str(row["versao"])) for row in rows), default=0)
    return Reply(
        body=json.dumps(rows, sort_keys=True).encode(),
        headers=(
            ("Content-Type", "application/json"),
            ("X-Max-Version", str(highest if maximum is None else maximum)),
            ("X-Total-Count", str(len(rows) if total is None else total)),
        ),
    )


def xml(index: int = 1, *, recipient: str = CLIENT) -> bytes:
    return nfe(recipient=recipient).replace(KEY.encode(), key(index).encode())


def national_item(index: int = 1, version: int = 8) -> tuple[dict[str, object], bytes]:
    return {
        "chave_nfse": key(index),
        "versao": version,
        "situacao": "autorizado",
        "data_emissao": "2026-09-15T12:30:00-03:00",
    }, nfse().replace(KEY.encode(), key(index).encode())


@dataclass
class CaptureWorld:
    setup: PortalSetup
    link: ClientProviderLink

    @property
    def tenant_id(self) -> UUID:
        return self.link.tenant_id

    def stream(
        self, collection: str = "nfe_recebidas", *, epoch: int = 1
    ) -> SyncStream:
        api = capture_api()
        with tenant_context(self.tenant_id):
            return cast(
                "SyncStream", api.ensure_stream(self.link, collection, epoch=epoch)
            )

    def reload(self, stream: SyncStream) -> SyncStream:
        with tenant_context(self.tenant_id):
            stream.refresh_from_db()
        return stream

    def ingest(
        self, *, version: int = 1, summary: dict[str, str] | None = None
    ) -> FiscalDocument:
        with patch(
            "apps.obligations.fiscal_ingest.scanner_from_settings",
            return_value=FakeScanner(),
        ):
            result = ingest_fiscal_document(
                tenant_id=self.tenant_id,
                client_id=self.link.client_id,
                source="captura",
                provider="focus",
                provider_version=str(version),
                environment="fake",
                client_role="destinatario",
                source_namespace="focus:nfe_recebidas",
                receipt_ref=f"synthetic-page-{version}",
                payload_bytes=None if summary is not None else xml(),
                summary=summary,
                actor=None,
                capture_binding_id=self.link.pk,
            )
        assert result is not None
        return result


def capture_world(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> CaptureWorld:
    # DateTimeField defaults retain the original timezone.now callable. Pin the
    # datetime it reads too, so an advanced service clock and every model default
    # use the same instant instead of scheduling calls against the wall clock.
    monkeypatch.setattr(timezone, "now", lambda: START)
    monkeypatch.setattr(timezone, "datetime", _CaptureDateTime)
    assert ProviderCall().next_attempt_at == START
    setup = ingest_setup(settings, tmp_path / "media")
    setup.graph.tenant.features = ["cycles_r1", "scan_required", "document_capture"]
    setup.graph.tenant.save(update_fields=["features"])
    settings.CLAMAV_ENABLED = True
    settings.PROVIDERS_PAUSED = False
    settings.FOCUS_PAUSED = False
    settings.FOCUS_ENVIRONMENT = "fake"
    settings.FOCUS_CAPTURE_PILOT_START_MONTH = "2026-09"
    settings.FOCUS_CAPTURE_LOCK_DIR = str(tmp_path / "capture-locks")
    settings.FOCUS_NFE_RECEBIDAS_QUALIFICATION_REF = "AXRM-PQ-FOCUS-1-synthetic"
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    reference = "focus/synthetic"
    with tenant_context(setup.graph.tenant.pk):
        connection = ProviderConnection.objects.create(
            tenant=setup.graph.tenant,
            provider="focus",
            environment="fake",
            secret_ref=reference,
            status="ativa",
        )
        link = ClientProviderLink.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            connection=connection,
            provider_company_ref=str(uuid4().int % 10**14),
            streams=["nfe_recebidas", "nfsen_recebidas"],
            capture_start_month=MONTH,
            certificate_cnpj=CLIENT,
            certificate_valid_from=date(2026, 1, 1),
            certificate_valid_to=date(2027, 1, 1),
            credential_generation=1,
            status="ativa",
            credential_state="ativa",
        )
        ProviderBudget.objects.create(
            tenant=setup.graph.tenant,
            provider="focus",
            window_start=MONTH,
            cap_microbrl=50_000_000,
            tariff_revision=FOCUS_PLAN,
        )
        for service in ("captura_nfe", "captura_nfse"):
            AuthorityRecord.objects.create(
                tenant=setup.graph.tenant,
                client=setup.graph.client,
                kind="autorizacao_captura",
                service=service,
                code="autorizacao_cliente",
                status="ativa",
                valid_from=MONTH,
                declared_by=setup.graph.actor,
                verified_by=setup.graph.actor,
            )
    return CaptureWorld(setup, link)


def fiscal_rows(world: CaptureWorld) -> list[FiscalDocument]:
    with tenant_context(world.tenant_id):
        return list(
            FiscalDocument.objects.select_related("current_revision").order_by(
                "document_key"
            )
        )
