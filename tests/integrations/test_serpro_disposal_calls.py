"""Guides and receipts are disposed of only on a resolved, complete date (AXRM-051 B-1).

A DAS guide is dated by the competence of the cycle its request was recorded for, a
PAGTOWEB receipt by the collection date of the payment it was asked for. Each lookup
stays inside the original's client. Identical bytes reused by a second acquisition,
a collection date outside its own consultation window, a request of another client
and authority evidence all keep the original before any claim exists.
"""

import base64
import json
from datetime import datetime, timedelta
from unittest.mock import patch
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from django.core.files.storage import default_storage
from django.utils import timezone

from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.cycles import retention_tasks
from apps.cycles.errors import IllegalTransition
from apps.cycles.retention_records import ContentPurgeClaim
from apps.cycles.retention_tasks import Candidate, disposal_refusal, purge_tenant
from apps.cycles.scanning import FakeScanner
from apps.cycles.tasks import scan_quarantine
from apps.integrations.authority import declare_authority, head_record
from apps.integrations.call_models import ProviderCall
from apps.integrations.models import FirmAuthorization
from apps.integrations.serpro import pgmei
from apps.integrations.serpro import retention as serpro_retention
from apps.integrations.serpro.payments import emit_payment_receipt
from apps.obligations.models import AcquisitionOrigin, Document, FederalPayment
from tests.fakes.provider import Reply
from tests.fakes.serpro import FakeSerpro
from tests.integrations.pagtoweb_support import PDF, World, page, receipt_body, world
from tests.integrations.pagtoweb_support import item as arrecadacao
from tests.integrations.pgmei_support import GuideWorld, das_reply, guide_world

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["guide_world", "world"]

SAO_PAULO = ZoneInfo("America/Sao_Paulo")


def _expiry(year: int) -> datetime:
    return datetime(year + 6, 1, 1, tzinfo=SAO_PAULO)


def _claimed(tenant_id: UUID, document_id: UUID) -> bool:
    with tenant_context(tenant_id):
        return ContentPurgeClaim.objects.filter(content_id=document_id).exists()


def _scan() -> None:
    with patch("apps.cycles.tasks.scanner_from_settings", return_value=FakeScanner()):
        scan_quarantine.apply().get()


def _sibling(tenant_id: UUID) -> ClientCompany:
    with tenant_context(tenant_id):
        client = ClientCompany.objects.first()
        assert client is not None
        return ClientCompany.objects.create(
            tenant=client.tenant, cnpj="11444777000161", legal_name="Sibling"
        )


def _payments(world: World, *items: dict[str, object]) -> list[FederalPayment]:
    world.script(page(list(items)))
    world.consult()
    with tenant_context(world.termo.tenant.pk):
        return list(FederalPayment.objects.order_by("numero_documento"))


def _receipt(world: World, payment: FederalPayment, pdf: bytes = PDF) -> ProviderCall:
    encoded = json.dumps({"pdf": base64.b64encode(pdf).decode()})
    world.script(Reply(body=receipt_body(encoded)))
    call = emit_payment_receipt(world.termo.tenant.pk, payment.pk, world.termo.actor.pk)
    assert call is not None
    assert call.state == "sucesso"
    return call


def _original(tenant_id: UUID, call: ProviderCall) -> Document:
    with tenant_context(tenant_id):
        return Document.objects.get(acquisition_receipt_ref=str(call.pk))


def _collected(day: str) -> dict[str, object]:
    entry = arrecadacao(f"DAS0000000{day.replace('-', '')}")
    entry["dataArrecadacao"] = f"{day}T00:00:00-03:00"
    return entry


def test_a_receipt_is_dated_by_its_payment_and_disposed_of_at_expiry(
    world: World,
) -> None:
    tenant_id = world.termo.tenant.pk
    [payment] = _payments(world, _collected("2026-10-20"))
    document = _original(tenant_id, _receipt(world, payment))
    _scan()
    with tenant_context(tenant_id):
        anchors, gap = retention_tasks._document_anchors(document)
    assert gap == ""
    assert [(a.basis, a.source_id) for a in anchors] == [
        ("arrecadacao_pagamento", payment.pk)
    ]
    expiry = _expiry(payment.data_arrecadacao.year)
    purge_tenant(tenant_id, dry_run=False, now=expiry - timedelta(seconds=1))
    assert not _claimed(tenant_id, document.pk)
    assert purge_tenant(tenant_id, dry_run=False, now=expiry)["purged"] == 1
    assert not default_storage.exists(document.storage_key)
    with tenant_context(tenant_id):
        assert FederalPayment.objects.filter(pk=payment.pk).exists()


def test_identical_receipt_bytes_for_two_payments_keep_the_original(
    world: World,
) -> None:
    tenant_id = world.termo.tenant.pk
    first, second = _payments(world, _collected("2026-09-15"), _collected("2026-10-20"))
    document = _original(tenant_id, _receipt(world, first))
    reused = _receipt(world, second)
    _scan()
    with tenant_context(tenant_id):
        # Intake kept one original for both calls; the second has none of its own.
        assert not Document.objects.filter(
            acquisition_receipt_ref=str(reused.pk)
        ).exists()
        anchors, gap = retention_tasks._document_anchors(document)
    assert gap == "proveniencia_incompleta"
    # The date that is known is still shown.
    assert [(a.basis, a.source_id) for a in anchors] == [
        ("arrecadacao_pagamento", first.pk)
    ]
    purge_tenant(tenant_id, dry_run=False, now=_expiry(2040))
    assert not _claimed(tenant_id, document.pk)
    assert default_storage.exists(document.storage_key)


def test_a_collection_date_outside_its_window_never_dates_a_receipt(
    world: World,
) -> None:
    tenant_id = world.termo.tenant.pk
    [payment] = _payments(world, _collected("2018-03-10"))
    document = _original(tenant_id, _receipt(world, payment))
    _scan()
    with tenant_context(tenant_id):
        anchors, gap = retention_tasks._document_anchors(document)
    assert (anchors, gap) == ((), "data_contraditoria")
    purge_tenant(tenant_id, dry_run=False, now=_expiry(2026))
    assert not _claimed(tenant_id, document.pk)
    assert default_storage.exists(document.storage_key)


def test_a_receipt_call_of_another_client_never_dates_a_payment(world: World) -> None:
    tenant_id = world.termo.tenant.pk
    [payment] = _payments(world, _collected("2026-10-20"))
    call = _receipt(world, payment)
    sibling = _sibling(tenant_id)
    stranger = ProviderCall(
        client_id=sibling.pk, operation=call.operation, request_key=call.request_key
    )
    with tenant_context(tenant_id):
        own = serpro_retention._receipt_anchors(call)
        foreign = serpro_retention._receipt_anchors(stranger)
    assert [a.source_id for a in own[0]] == [payment.pk]
    assert foreign == ([], ["pagamento_ausente"])


@pytest.mark.parametrize("use", ["authority", "termo"])
def test_authority_evidence_is_never_ordinary_disposal(world: World, use: str) -> None:
    termo = world.termo
    with tenant_context(termo.tenant.pk):
        document = Document.objects.create(
            tenant=termo.tenant,
            client=termo.client,
            original_filename="procuracao.pdf",
            content_type="application/pdf",
            byte_size=10,
            sha256="d" * 64,
            uploaded_by=termo.actor,
            acquisition_origin=AcquisitionOrigin.UPLOAD_STAFF,
            portal_visible=False,
        )
        candidate = Candidate(termo.client.pk, "document", document.pk, None)
        assert disposal_refusal(candidate) == ""
        if use == "authority":
            head = head_record(termo.client.pk, "sitfis")
            declare_authority(
                termo.actor,
                termo.client.pk,
                service="sitfis",
                code="00002",
                valid_from=timezone.localdate(),
                expected_version=head.version if head else 0,
                evidence_document=document,
            )
        else:
            FirmAuthorization.objects.create(
                tenant=termo.tenant,
                kind="termo_autorizacao_serpro",
                signed_document=document,
                submitted_at=timezone.now(),
                status="enviado",
            )
        assert disposal_refusal(candidate) == "evidencia_de_autorizacao"


def _obtain(world: GuideWorld) -> tuple[ProviderCall, Document]:
    with FakeSerpro((das_reply(),)) as peer:
        outcome = pgmei.obtain_das_guide(
            world.owner, world.tenant_id, world.cycle.pk, origin=peer.origin
        )
    assert outcome.document_id is not None
    with tenant_context(world.tenant_id):
        return (
            ProviderCall.objects.get(pk=outcome.call_id),
            Document.objects.get(pk=outcome.document_id),
        )


def test_a_guide_request_of_another_client_never_dates_a_guide(
    guide_world: GuideWorld,
) -> None:
    call, _document = _obtain(guide_world)
    sibling = _sibling(guide_world.tenant_id)
    stranger = ProviderCall(
        client_id=sibling.pk, operation=call.operation, request_key=call.request_key
    )
    with tenant_context(guide_world.tenant_id):
        own = serpro_retention._guide_anchors(call)
        foreign = serpro_retention._guide_anchors(stranger)
    assert [(a.basis, a.source_id) for a in own[0]] == [
        ("competencia_guia", guide_world.cycle.pk)
    ]
    assert foreign == ([], ["pedido_sem_ciclo"])


def test_a_cycle_link_does_not_erase_an_unresolved_federal_reference(
    guide_world: GuideWorld,
) -> None:
    _call, document = _obtain(guide_world)
    with tenant_context(guide_world.tenant_id):
        anchors, gap = retention_tasks._document_anchors(document)
        assert (gap, {a.basis for a in anchors}) == (
            "",
            {"ciclo", "competencia_guia"},
        )
        document.acquisition_receipt_ref = "sitfis-call-001"
        anchors, gap = retention_tasks._document_anchors(document)
    assert gap == "recibo_invalido"
    assert {a.basis for a in anchors} == {"ciclo"}


def test_a_guide_still_in_quarantine_is_kept(
    guide_world: GuideWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    def closed(*args: object, **kwargs: object) -> None:
        message = "Este ciclo está encerrado."
        raise IllegalTransition(message)

    monkeypatch.setattr(pgmei, "_link_in_cycle", closed)
    _call, document = _obtain(guide_world)
    expiry = _expiry(guide_world.cycle.period_start.year)
    with tenant_context(guide_world.tenant_id):
        candidate = Candidate(document.client_id, "document", document.pk, expiry)
        assert disposal_refusal(candidate) == "verificacao_pendente"
    purge_tenant(guide_world.tenant_id, dry_run=False, now=expiry)
    assert not _claimed(guide_world.tenant_id, document.pk)
