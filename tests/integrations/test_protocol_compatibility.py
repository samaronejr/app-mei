"""Bytewise ordinary-operation equivalence against frozen 0e79391 replay code."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from django.core.files.storage import default_storage
from django.core.serializers.json import DjangoJSONEncoder
from django.test import override_settings
from django.utils import timezone

from apps.audit.models import Event
from apps.core.tenancy import tenant_context
from apps.integrations import (
    call_contracts,
    call_dispatch,
    provider_journal,
    provider_replay,
)
from apps.integrations.budget import BudgetPolicy
from apps.integrations.budget_models import ProviderBudget, ProviderReservation
from apps.integrations.budget_pricing import FOCUS_PLAN
from apps.integrations.call_contracts import CallResult, Operation, Reservation
from apps.integrations.call_dispatch import Dispatch
from apps.integrations.call_models import (
    CallState,
    FirmProviderCall,
    FirmProviderCallAttempt,
    ProviderCall,
    ProviderCallAttempt,
    ProviderEffectBarrier,
)
from apps.integrations.call_protocol import claim, perform, reserve
from apps.integrations.firm_calls import firm_claim, firm_perform
from apps.integrations.journal_schema import JournalRecord
from apps.integrations.models import (
    ClientProviderLink,
    FirmAuthorization,
    IntegrationIssue,
)
from apps.integrations.serpro.models import (
    FirmIntegrationIssue,
    FirmProviderReservation,
)
from apps.tenants.models import Tenant, UsageRecord
from tests.integrations.axrm016_baseline import journal as before_journal
from tests.integrations.axrm016_baseline import replay as before_replay
from tests.integrations.axrm016_baseline import writer as before_writer
from tests.integrations.provider_peer import ProviderPeer
from tests.integrations.test_call_protocol import (
    SyntheticPolicy,
    provider_link,
    request,
)
from tests.integrations.test_firm_termo_journal import reserve as reserve_termo
from tests.integrations.test_firm_termo_journal import termo

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from typing import Literal
    from uuid import UUID

    from django.db.models import Model
    from pytest_django.fixtures import SettingsWrapper

    from apps.integrations.journal_schema import FirmJournalRecord

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["provider_link", "termo"]

SCOPED_MODELS = (
    ProviderBudget,
    ProviderReservation,
    ProviderCall,
    ProviderCallAttempt,
    ProviderEffectBarrier,
    FirmProviderCall,
    FirmProviderCallAttempt,
    FirmProviderReservation,
    FirmAuthorization,
    UsageRecord,
    IntegrationIssue,
    FirmIntegrationIssue,
    Event,
)


class UnbilledReadPolicy(SyntheticPolicy):
    """Declare a synthetic free-read contract without inventing a priced tariff."""

    def reserve(self, call: ProviderCall) -> Reservation:
        return Reservation(0, "synthetic-unbilled-v1")


def test_baseline_fixture_matches_the_recorded_protocol_provenance() -> None:
    root = Path(__file__).with_name("axrm016_baseline")
    provenance = json.loads((root / "provenance.json").read_text())
    assert provenance["commit"] == "0e79391"
    for name, receipt in provenance["files"].items():
        assert (
            sha256((root / f"{name}.py").read_bytes()).hexdigest()
            == receipt["reference_sha256"]
        )


def _snapshot(tenant_id: UUID) -> dict[str, list[dict[str, object]]]:
    with tenant_context(tenant_id):
        return {
            model._meta.label_lower: [
                dict(row)
                for row in model._default_manager.filter(tenant_id=tenant_id)
                .order_by("pk")
                .values()
            ]
            for model in SCOPED_MODELS
        }


def _bytes(snapshot: dict[str, list[dict[str, object]]]) -> bytes:
    return json.dumps(snapshot, sort_keys=True, cls=DjangoJSONEncoder).encode()


def _ordinary_windows[**P, T](
    callback: Callable[P, T], seen: list[str]
) -> Callable[P, T]:
    def observed(*args: P.args, **kwargs: P.kwargs) -> T:
        assert kwargs.get("guard") is None
        seen.append(str(args[2]))
        return callback(*args, **kwargs)

    return observed


def _reset_call(
    model: type[Model], tenant_id: UUID, before: list[dict[str, object]]
) -> None:
    with tenant_context(tenant_id):
        for row in before:
            assert model._default_manager.filter(pk=row["id"]).update(**row) == 1


def _writer_bytes(
    records: Sequence[JournalRecord | FirmJournalRecord],
    directory: Path,
    append: Callable[[JournalRecord | FirmJournalRecord], str],
) -> list[bytes]:
    settings = {
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(directory)},
        }
    }
    with override_settings(STORAGES=settings):
        result = []
        for record in records:
            path = append(record)
            with default_storage.open(path, "rb") as stream:
                result.append(stream.read())
        return result


@pytest.mark.parametrize(
    "case",
    [
        ("SP-PAG", "read", "consulta", "serpro"),
        ("SP-DAS", "issuance", "emissao", "serpro"),
        ("SP-COMP", "issuance", "emissao", "serpro"),
        ("FC-NFE", "read", "incluido_no_plano", "focus"),
        ("SP-FREE", "read", "nao_faturada", "serpro"),
    ],
)
def test_non_sitfis_journal_writer_and_replay_are_byte_identical(
    provider_link: ClientProviderLink,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case: tuple[
        str,
        Literal["read", "issuance"],
        Literal["consulta", "emissao", "incluido_no_plano", "nao_faturada"],
        Literal["serpro", "focus"],
    ],
) -> None:
    code, kind, billing, provider = case
    windows: list[str] = []
    monkeypatch.setattr(
        call_dispatch,
        "_wire_window",
        _ordinary_windows(call_dispatch._wire_window, windows),
    )
    fixed = timezone.now() + timedelta(days=1)
    monkeypatch.setattr(timezone, "now", lambda: fixed)
    settings.USAGE_UNIT_PRICES_BRL = {
        "serpro_consulta": "0.24",
        "serpro_emissao": "0.32",
        "focus_documentos": "1.00",
    }
    settings.PROVIDER_GLOBAL_MONTHLY_CAP_BRL = {
        "serpro": "10.00",
        "focus": "10.00",
    }
    Tenant.objects.filter(pk=provider_link.tenant_id).update(
        features=["cycles_r1", "scan_required", "federal_checks", "document_capture"]
    )
    with tenant_context(provider_link.tenant_id):
        provider_link.connection.provider = provider
        provider_link.connection.save()
        ClientProviderLink.objects.filter(pk=provider_link.pk).update(
            credential_state="ativa"
        )
        ProviderBudget.objects.create(
            tenant_id=provider_link.tenant_id,
            provider=provider,
            window_start=fixed.date().replace(day=1),
            cap_microbrl=5_000_000,
            tariff_revision=FOCUS_PLAN
            if provider == "focus"
            else "synthetic-tariff-v1",
            included_documents=1,
        )
    operation = Operation(
        code,
        provider,
        "v1",
        kind,
        "document_capture" if provider == "focus" else "federal_checks",
        billing,
        UnbilledReadPolicy()
        if billing == "nao_faturada"
        else BudgetPolicy(lambda call: True),
    )
    monkeypatch.setitem(call_contracts._OPERATIONS, code, operation)
    assert operation.wire_guard is None
    call = reserve(
        provider_link.tenant_id,
        provider_link.pk,
        replace(request(code), operation=code),
    )
    ticket = claim(provider_link.tenant_id, call.pk)
    assert ticket is not None
    with ProviderPeer() as peer:
        perform(ticket, peer)
        assert peer.requests == [ticket.owner.hex]
    assert windows == [code]
    records = provider_journal.read(provider_link.tenant_id)
    initial = _snapshot(provider_link.tenant_id)
    if not operation.journaled:
        assert records == []
        with tenant_context(provider_link.tenant_id):
            persisted = ProviderCall.objects.get(pk=call.pk)
            receipt = JournalRecord.started(persisted, ticket)
        dispatch = Dispatch(ticket, receipt, journaled=False)
        ordinary = CallResult(CallState.SUCCESS, 200, "ok", "nao")
        before_writer.record_result(dispatch, ordinary)
        assert _bytes(_snapshot(provider_link.tenant_id)) == _bytes(initial)
        call_dispatch.record_result(dispatch, ordinary)
        assert _bytes(_snapshot(provider_link.tenant_id)) == _bytes(initial)
        assert provider_journal.read(provider_link.tenant_id) == []
        return
    old_bytes = _writer_bytes(records, tmp_path / "old-writer", before_journal.append)
    assert (
        _writer_bytes(records, tmp_path / "new-writer", provider_journal.append)
        == old_bytes
    )
    assert _bytes(_snapshot(provider_link.tenant_id)) == _bytes(initial)
    with tenant_context(provider_link.tenant_id):
        before_replay._restore(records)
    expected = _snapshot(provider_link.tenant_id)
    for label, rows in initial.items():
        if label != ProviderCall._meta.label_lower:
            assert rows == expected[label]
    _reset_call(
        ProviderCall,
        provider_link.tenant_id,
        initial[ProviderCall._meta.label_lower],
    )
    with tenant_context(provider_link.tenant_id):
        provider_replay._restore(records)
    actual = _snapshot(provider_link.tenant_id)
    assert _bytes(actual) == _bytes(expected)


def test_ordinary_termo_writer_and_replay_are_byte_identical(
    termo: FirmAuthorization, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    windows: list[str] = []
    monkeypatch.setattr(
        call_dispatch,
        "_wire_window",
        _ordinary_windows(call_dispatch._wire_window, windows),
    )
    fixed = timezone.now() + timedelta(days=1)
    monkeypatch.setattr(timezone, "now", lambda: fixed)
    call = reserve_termo(termo)
    ticket = firm_claim(termo.tenant_id, call.pk)
    assert ticket is not None
    with ProviderPeer() as peer:
        firm_perform(ticket, peer)
    assert windows == ["SP-TERMO"]
    records = provider_journal.read_firm(termo.tenant_id)
    initial = _snapshot(termo.tenant_id)
    old_bytes = _writer_bytes(records, tmp_path / "old-firm", before_journal.append)
    assert (
        _writer_bytes(records, tmp_path / "new-firm", provider_journal.append)
        == old_bytes
    )
    with tenant_context(termo.tenant_id):
        before_replay._restore_firm(records)
    expected = _snapshot(termo.tenant_id)
    for label, rows in initial.items():
        if label != FirmProviderCall._meta.label_lower:
            assert rows == expected[label]
    _reset_call(
        FirmProviderCall, termo.tenant_id, initial[FirmProviderCall._meta.label_lower]
    )
    with tenant_context(termo.tenant_id):
        provider_replay._restore_firm(records)
    assert _bytes(_snapshot(termo.tenant_id)) == _bytes(expected)
