"""S15 B: a client uploads another company's document (T13).

Given a PDF or XML whose CNPJ is not the cycle client's, then the intake is
`divergente`, the link does not count as sent, a `documento_de_outra_empresa`
exception opens on the cycle, and the document reaches no other client -- not even
the firm's other client whose CNPJ it carries. It is never reassigned. "Not
searchable" (T21) and "not shadow-queued" (T24) are asserted by those todos.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from threading import Barrier, Event
from uuid import UUID

import pytest
from django.core.files.storage import default_storage
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.evidence import (
    DocumentIntake,
    EvidenceLink,
    ExtractedField,
    IntakeState,
    counted_links,
)
from apps.cycles.extraction import DIVERGENT, UNVERIFIABLE
from apps.cycles.extraction import service as extraction_service
from apps.cycles.extraction.service import (
    EXCEPTION_REASON,
    ExtractionOutcome,
    claim_extraction,
    due_divergent_links,
    due_extractions,
    extract_intake,
    record_extraction,
    run_extraction,
)
from apps.cycles.models import (
    CycleEvent,
    EvidenceRequest,
    ExceptionInternal,
    ServiceCycle,
)
from apps.cycles.models import Exception as CycleException
from apps.cycles.services.cycle import open_cycle
from apps.cycles.services.requests import RequestFields, create_request, send_requests
from apps.cycles.tasks import extract_documents, propagate_document_link
from apps.obligations.models import Document
from apps.tenants.models import Tenant, TenantRole
from config import celery_app
from tests.cycles.document_fixtures import (
    OTHER_CLIENT_CNPJ,
    fixture,
    reviewed,
    scan_clean,
    submit,
)
from tests.portal.request_fixtures import PortalSetup, portal_setup, upload
from tests.support_threads import run_in_thread_as_runtime
from tests.ui.factories import add_member

DB = pytest.mark.django_db(transaction=True)
PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 64


@pytest.fixture
def setup(settings: SettingsWrapper, tmp_path: Path) -> PortalSetup:
    settings.MEDIA_ROOT = tmp_path
    return portal_setup("t13-wrong-company")


def _exceptions(setup: PortalSetup) -> list[CycleException]:
    with tenant_context(setup.graph.tenant.pk):
        return list(CycleException.objects.filter(reason_code=EXCEPTION_REASON))


@DB
@pytest.mark.parametrize("name", ["nfe_outra_empresa.xml", "recibo_outra_empresa.pdf"])
def test_other_company_document_is_divergent_suspended_and_raises_an_exception(
    setup: PortalSetup, name: str
) -> None:
    intake, link = reviewed(setup, name)

    assert intake.state == IntakeState.DIVERGENT
    assert intake.reason == DIVERGENT
    with tenant_context(setup.graph.tenant.pk):
        # Not linked: the link exists (the record of what was sent) but is suspended.
        assert EvidenceLink.objects.filter(pk=link.pk).exists()
        assert not counted_links().filter(pk=link.pk).exists()
        document = Document.objects.get(pk=intake.document_id)
        # Never reassigned: the document, its intake and its fields stay with the
        # client that sent it, and nothing was written under the other client.
        assert document.client_id == setup.graph.client.pk
        assert not Document.objects.filter(client=setup.other_client).exists()
        assert not EvidenceLink.objects.filter(client=setup.other_client).exists()
        assert not DocumentIntake.objects.filter(client=setup.other_client).exists()
        assert not ExtractedField.objects.filter(client=setup.other_client).exists()
        assert OTHER_CLIENT_CNPJ in set(
            ExtractedField.objects.filter(document=document).values_list(
                "value", flat=True
            )
        )
    (problem,) = _exceptions(setup)
    assert problem.cycle_id == setup.cycle.pk
    assert problem.subject_ref == intake.pk
    with tenant_context(setup.graph.tenant.pk):
        assert ExceptionInternal.objects.get(exception=problem).evidence_refs == [
            str(intake.document_id)
        ]
    assert problem.state == "aberta"
    assert problem.blocks_completion is True
    assert problem.client_visible is False

    # The uploader's portal no longer counts it as sent, nor serves it.
    portal = setup.portal()
    page = portal.get(f"/solicitacoes/{setup.request.pk}/").content.decode()
    assert 'data-status="recebido"' not in page
    assert portal.get(f"/documentos/{document.storage_key}").status_code == 404
    assert name not in portal.get("/documentos/").content.decode()

    # The firm's other client -- whose CNPJ the document carries -- sees nothing.
    stranger = add_member(
        setup.graph.tenant,
        "dono@outra-empresa.test",
        TenantRole.CLIENT_OWNER,
        client=setup.other_client,
    )
    other_portal = setup.portal(stranger)
    assert name not in other_portal.get("/documentos/").content.decode()
    assert other_portal.get(f"/documentos/{document.storage_key}").status_code == 404
    assert other_portal.get(f"/solicitacoes/{setup.request.pk}/").status_code == 404

    # The firm sees it in the divergent queue.
    queue = setup.firm().get("/documentos/?fila=divergentes").content.decode()
    assert f'data-link-id="{link.pk}"' in queue
    assert 'data-intake-state="divergente"' in queue


@DB
@pytest.mark.parametrize(
    "name", ["nfe_emitente_cliente.xml", "nfse_nacional.xml", "recibo_cliente.pdf"]
)
def test_client_document_with_a_counterpart_is_not_divergent(
    setup: PortalSetup, name: str
) -> None:
    intake, link = reviewed(setup, name)

    assert intake.state == IntakeState.CLASSIFIED
    assert _exceptions(setup) == []
    with tenant_context(setup.graph.tenant.pk):
        assert counted_links().filter(pk=link.pk).exists()


@DB
def test_image_is_not_verifiable_and_never_divergent(setup: PortalSetup) -> None:
    intake, _link = reviewed(setup, "foto-nota.png", PNG_BYTES)

    assert intake.state == IntakeState.CLASSIFIED
    assert intake.reason == UNVERIFIABLE
    assert _exceptions(setup) == []
    page = setup.firm().get("/documentos/").content.decode()
    assert "foto-nota.png" in page


@DB
def test_repeat_extraction_does_not_open_a_second_exception(
    setup: PortalSetup,
) -> None:
    intake, _link = reviewed(setup, "nfe_outra_empresa.xml")

    extract_intake(setup.graph.tenant.pk, intake.pk, now=timezone.now())

    assert len(_exceptions(setup)) == 1


def _later_request(setup: PortalSetup, month: int) -> EvidenceRequest:
    with tenant_context(setup.graph.tenant.pk):
        ref = open_cycle(
            setup.graph.actor,
            setup.graph.engagement.pk,
            date(2026, month, 1),
            setup.graph.definition.pk,
            idempotency_key=f"relink:{month}",
        )
        request = create_request(
            setup.graph.actor,
            ref.id,
            RequestFields(title="Documento do período"),
            expected_version=ref.version,
        )
        cycle = ServiceCycle.objects.get(pk=ref.id)
        send_requests(setup.graph.actor, cycle.pk, expected_version=cycle.version)
    return request


@pytest.fixture
def eager_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the real beat dispatch and task bodies, without a broker timing race."""
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    monkeypatch.setattr(celery_app.conf, "task_eager_propagates", True)


@DB
@pytest.mark.parametrize("name", ["nfe_outra_empresa.xml", "recibo_outra_empresa.pdf"])
def test_late_relinks_get_one_exception_per_cycle_without_reextraction(
    setup: PortalSetup, name: str, eager_tasks: None
) -> None:
    intake, _link = reviewed(setup, name)
    tid = setup.graph.tenant.pk
    portal = setup.portal()
    with tenant_context(tid):
        fields = set(ExtractedField.objects.values_list("pk", flat=True))
        extracted = set(
            CycleEvent.objects.filter(type="document.extracted").values_list(
                "pk", flat=True
            )
        )
    cycles = [setup.cycle.pk]
    for month in (10, 11):
        request = _later_request(setup, month)
        cycles.append(request.cycle_id)
        with tenant_context(tid):
            before = ServiceCycle.objects.get(pk=request.cycle_id).version
        for _ in range(2):
            assert (
                portal.post(
                    f"/solicitacoes/{request.pk}/enviar",
                    {"file": upload(name, fixture(name))},
                ).status_code
                == 302
            )
            assert extract_intake(tid, intake.pk, now=timezone.now()).value == "skipped"
            assert due_extractions(tid) == []
            extract_documents()
        with tenant_context(tid):
            assert [
                CycleException.objects.filter(
                    cycle_id=cid, reason_code=EXCEPTION_REASON, subject_ref=intake.pk
                ).count()
                for cid in cycles
            ] == [1] * len(cycles)
            assert ServiceCycle.objects.get(pk=request.cycle_id).version == before + 1
            assert (
                CycleEvent.objects.filter(
                    cycle_id=request.cycle_id, type="exception.opened"
                ).count()
                == 1
            )
    assert extract_documents() == 0
    with tenant_context(tid):
        assert Document.objects.count() == DocumentIntake.objects.count() == 1
        assert EvidenceLink.objects.count() == 3
        assert not counted_links().exists()
        fresh = DocumentIntake.objects.get(pk=intake.pk)
        assert (fresh.state, fresh.version) == (intake.state, intake.version)
        assert fresh.client_id == fresh.document.client_id == setup.graph.client.pk
        assert set(ExtractedField.objects.values_list("pk", flat=True)) == fields
        assert (
            set(
                CycleEvent.objects.filter(type="document.extracted").values_list(
                    "pk", flat=True
                )
            )
            == extracted
        )
    assert portal.get(f"/documentos/{intake.document.storage_key}").status_code == 404


@DB
@pytest.mark.parametrize("link_before_snapshot", [True, False])
def test_relink_during_classification_converges_on_the_normal_sweep(
    setup: PortalSetup,
    eager_tasks: None,
    monkeypatch: pytest.MonkeyPatch,
    link_before_snapshot: bool,
) -> None:
    """Commit a portal link on either side of classification's link snapshot."""
    name = "nfe_outra_empresa.xml"
    intake = submit(setup, name)
    scan_clean(setup, intake)
    tid = setup.graph.tenant.pk
    request = _later_request(setup, 10)
    ticket = claim_extraction(tid, intake.pk)
    assert ticket is not None
    extraction = run_extraction(ticket, default_storage)
    at_snapshot, release = Event(), Event()
    linked_cycles = extraction_service._linked_cycles

    def snapshot(candidate: DocumentIntake) -> list[tuple[ServiceCycle, UUID]]:
        if link_before_snapshot:
            at_snapshot.set()
            assert release.wait(timeout=20)
        result = linked_cycles(candidate)
        if not link_before_snapshot:
            at_snapshot.set()
            assert release.wait(timeout=20)
        return result

    def classify() -> ExtractionOutcome:
        return record_extraction(ticket, extraction, now=timezone.now())

    monkeypatch.setattr(extraction_service, "_linked_cycles", snapshot)
    with ThreadPoolExecutor(max_workers=1) as pool:
        finalization = pool.submit(run_in_thread_as_runtime, tid, classify)
        try:
            assert at_snapshot.wait(timeout=20)
            assert (
                setup.portal()
                .post(
                    f"/solicitacoes/{request.pk}/enviar",
                    {"file": upload(name, fixture(name))},
                )
                .status_code
                == 302
            )
        finally:
            release.set()
        assert finalization.result(timeout=30) is ExtractionOutcome.DIVERGENT

    with tenant_context(tid):
        before = CycleException.objects.filter(cycle_id=request.cycle_id).count()
    assert before == int(link_before_snapshot)
    assert extract_documents() == int(not link_before_snapshot)
    assert extract_documents() == 0
    with tenant_context(tid):
        assert CycleException.objects.filter(subject_ref=intake.pk).count() == 2
        assert CycleEvent.objects.filter(type="exception.opened").count() == 2
        assert CycleEvent.objects.filter(type="document.extracted").count() == (
            2 if link_before_snapshot else 1
        )
        assert ExtractedField.objects.count() == 5
        assert not counted_links().exists()


def _relink(setup: PortalSetup, request: EvidenceRequest) -> EvidenceLink:
    name = "nfe_outra_empresa.xml"
    assert (
        setup.portal()
        .post(
            f"/solicitacoes/{request.pk}/enviar",
            {"file": upload(name, fixture(name))},
        )
        .status_code
        == 302
    )
    with tenant_context(setup.graph.tenant.pk):
        return EvidenceLink.objects.get(request=request)


@DB
def test_two_concurrent_late_links_on_one_cycle_emit_once(
    setup: PortalSetup,
) -> None:
    intake, _link = reviewed(setup, "nfe_outra_empresa.xml")
    links = [_relink(setup, _later_request(setup, 10)) for _ in range(2)]
    tid = setup.graph.tenant.pk
    barrier = Barrier(2, timeout=20)

    def reconcile(link: EvidenceLink) -> bool:
        barrier.wait()
        return bool(propagate_document_link(str(tid), str(link.pk)))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                run_in_thread_as_runtime, tid, lambda link=link: reconcile(link)
            )
            for link in links
        ]
        assert sorted(future.result(timeout=30) for future in futures) == [False, True]
    with tenant_context(tid):
        assert CycleException.objects.filter(subject_ref=intake.pk).count() == 2
        assert CycleEvent.objects.filter(type="exception.opened").count() == 2
        assert CycleEvent.objects.filter(type="document.extracted").count() == 1


@DB
def test_recorded_cycles_do_not_starve_late_links_or_reopen_resolved_findings(
    setup: PortalSetup,
) -> None:
    intake, initial = reviewed(setup, "nfe_outra_empresa.xml")
    tid = setup.graph.tenant.pk
    with tenant_context(tid):
        CycleException.objects.filter(subject_ref=intake.pk).update(state="resolvida")
    links = [_relink(setup, _later_request(setup, month)) for month in (10, 11)]
    for link in links:
        assert due_divergent_links(tid, limit=1) == [link.pk]
        assert propagate_document_link(str(tid), str(link.pk)) is True
        assert propagate_document_link(str(tid), str(link.pk)) is False
    assert propagate_document_link(str(tid), str(initial.pk)) is False
    assert due_divergent_links(tid, limit=1) == []
    with tenant_context(tid):
        assert CycleException.objects.filter(subject_ref=intake.pk).count() == 3
        assert CycleEvent.objects.filter(type="exception.opened").count() == 3


@DB
def test_different_divergent_intakes_on_one_later_cycle_each_get_a_finding(
    setup: PortalSetup, eager_tasks: None
) -> None:
    names = ("nfe_outra_empresa.xml", "recibo_outra_empresa.pdf")
    intakes = [reviewed(setup, name)[0] for name in names]
    request = _later_request(setup, 10)
    portal = setup.portal()
    for name, intake in zip(names, intakes, strict=True):
        assert (
            portal.post(
                f"/solicitacoes/{request.pk}/enviar",
                {"file": upload(name, fixture(name))},
            ).status_code
            == 302
        )
        assert extract_documents() == 1
        with tenant_context(setup.graph.tenant.pk):
            assert (
                CycleException.objects.filter(
                    cycle_id=request.cycle_id,
                    reason_code=EXCEPTION_REASON,
                    subject_ref=intake.pk,
                ).count()
                == 1
            )
    assert extract_documents() == 0
    with tenant_context(setup.graph.tenant.pk):
        assert CycleException.objects.count() == 4
        assert CycleEvent.objects.filter(type="exception.opened").count() == 4
        assert CycleEvent.objects.filter(type="document.extracted").count() == 2


@DB
def test_stale_relink_task_after_third_party_acceptance_writes_nothing(
    setup: PortalSetup,
) -> None:
    intake, initial = reviewed(setup, "nfe_outra_empresa.xml")
    tid = setup.graph.tenant.pk
    link = _relink(setup, _later_request(setup, 10))
    assert due_divergent_links(tid) == [link.pk]
    response = setup.firm().post(
        f"/documentos/{initial.pk}/revisar/",
        {
            "decision": "aceito",
            "expected_version": setup.reload(setup.request).version,
            "third_party": "1",
            "override_reason": "Documento de fornecedor necessário.",
        },
    )
    assert response.status_code == 302
    assert propagate_document_link(str(tid), str(link.pk)) is False
    assert due_divergent_links(tid) == []
    with tenant_context(tid):
        assert DocumentIntake.objects.get(pk=intake.pk).state == IntakeState.LINKED
        assert CycleException.objects.filter(subject_ref=intake.pk).count() == 1
        assert CycleEvent.objects.filter(type="exception.opened").count() == 1


@DB
def test_relink_sweep_preserves_flag_and_tenant_boundaries(
    setup: PortalSetup, eager_tasks: None
) -> None:
    intake, _initial = reviewed(setup, "nfe_outra_empresa.xml")
    link = _relink(setup, _later_request(setup, 10))
    tenant = setup.graph.tenant
    other = Tenant.objects.create(
        name="Outra firma", slug="relink-other-firm", features=["cycles_r1"]
    )
    assert due_divergent_links(other.pk) == []
    assert propagate_document_link(str(other.pk), str(link.pk)) is False
    tenant.features = []
    tenant.save(update_fields=["features"])
    assert extract_documents() == 0
    with tenant_context(tenant.pk):
        assert CycleException.objects.filter(subject_ref=intake.pk).count() == 1
    tenant.features = ["cycles_r1"]
    tenant.save(update_fields=["features"])
    assert extract_documents() == 1
    with tenant_context(tenant.pk):
        assert CycleException.objects.filter(subject_ref=intake.pk).count() == 2
