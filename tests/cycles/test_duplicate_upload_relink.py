"""S15 C: the same bytes twice give one Document and two links, with no error."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from pathlib import Path
from threading import Barrier, local
from typing import TYPE_CHECKING

import pytest
from django.db import connection, connections
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles import intake
from apps.cycles.models import (
    CycleEvent,
    DocumentIntake,
    EvidenceLink,
    EvidenceRequest,
    RequestInternal,
    RequestState,
)
from apps.obligations.models import Document
from tests.portal.request_fixtures import PortalSetup, portal_setup, upload

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.django_db(transaction=True)

PAYLOAD = b"%PDF-1.4 the very same bytes\n"


@pytest.fixture
def setup(settings: SettingsWrapper, tmp_path: Path) -> PortalSetup:
    settings.MEDIA_ROOT = tmp_path
    return portal_setup("relink")


def _second_request(setup: PortalSetup) -> EvidenceRequest:
    with tenant_context(setup.graph.tenant.pk):
        second = EvidenceRequest.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            cycle=setup.cycle,
            title="Comprovante de faturamento",
            state=RequestState.REQUESTED,
            created_by=setup.graph.actor,
        )
        RequestInternal.objects.create(
            tenant=setup.graph.tenant, client=setup.graph.client, request=second
        )
    return second


def test_same_bytes_to_two_requests_reuse_one_document(setup: PortalSetup) -> None:
    second = _second_request(setup)
    browser = setup.portal()
    first = browser.post(
        f"/solicitacoes/{setup.request.pk}/enviar",
        {"file": upload("nota.pdf", PAYLOAD)},
    )
    again = browser.post(
        f"/solicitacoes/{second.pk}/enviar",
        {"file": upload("outro-nome.pdf", PAYLOAD)},
        headers={"HX-Request": "true"},
    )
    assert first.status_code == HTTPStatus.FOUND
    assert again.status_code == HTTPStatus.OK
    assert "vinculado a esta solicitação" in again.content.decode()
    with tenant_context(setup.graph.tenant.pk):
        assert Document.objects.count() == 1
        document = Document.objects.get()
        links = list(EvidenceLink.objects.order_by("created_at", "pk"))
        assert DocumentIntake.objects.count() == 1
        events = CycleEvent.objects.filter(type="evidence.submitted").count()
    assert [link.document_id for link in links] == [document.pk, document.pk]
    assert [link.request_id for link in links] == [setup.request.pk, second.pk]
    assert [link.relinked for link in links] == [False, True]
    assert events == len(links)


def test_concurrent_identical_uploads_leave_only_the_claimed_object(
    setup: PortalSetup, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    second = _second_request(setup)
    admitted = Barrier(2, timeout=20)
    document_for: Callable[..., tuple[Document, bool]] = intake.document_for
    thread_state = local()

    def wait_at_first_admission(**kwargs: object) -> tuple[Document, bool]:
        try:
            return document_for(**kwargs)
        except intake.QuarantineRequiredError:
            if not getattr(thread_state, "admitted", False):
                thread_state.admitted = True
                admitted.wait()
            raise

    monkeypatch.setattr(intake, "document_for", wait_at_first_admission)

    def send(request: EvidenceRequest) -> None:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET ROLE app_runtime")
            response = setup.portal().post(
                f"/solicitacoes/{request.pk}/enviar",
                {"file": upload("nota.pdf", PAYLOAD)},
                headers={"HX-Request": "true"},
            )
            assert response.status_code == HTTPStatus.OK
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(send, request) for request in (setup.request, second)]
        for future in futures:
            future.result(timeout=45)

    with tenant_context(setup.graph.tenant.pk):
        document = Document.objects.get()
        claim = DocumentIntake.objects.get(document=document).quarantine_key
        links = list(EvidenceLink.objects.filter(document=document))
    assert {link.request_id for link in links} == {setup.request.pk, second.pk}
    assert sorted(link.relinked for link in links) == [False, True]
    stored = {
        p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()
    }
    assert stored == {claim}


def test_same_bytes_to_the_same_request_again_changes_nothing(
    setup: PortalSetup,
) -> None:
    browser = setup.portal()
    url = f"/solicitacoes/{setup.request.pk}/enviar"
    assert browser.post(url, {"file": upload("a.pdf", PAYLOAD)}).status_code == 302
    repeat = browser.post(
        url, {"file": upload("a.pdf", PAYLOAD)}, headers={"HX-Request": "true"}
    )
    assert repeat.status_code == HTTPStatus.OK
    assert "já tinha sido enviado para esta solicitação" in repeat.content.decode()
    with tenant_context(setup.graph.tenant.pk):
        assert Document.objects.count() == 1
        assert EvidenceLink.objects.count() == 1
        assert CycleEvent.objects.filter(type="evidence.submitted").count() == 1


def test_dedup_is_per_client_never_across_clients(setup: PortalSetup) -> None:
    """The other client holding the same bytes must not be reused (or revealed)."""
    with tenant_context(setup.graph.tenant.pk):
        foreign = Document.objects.create(
            tenant=setup.graph.tenant,
            client=setup.other_client,
            sha256=hashlib.sha256(PAYLOAD).hexdigest(),
            original_filename="foreign.pdf",
            content_type="application/pdf",
            byte_size=len(PAYLOAD),
            uploaded_by=setup.graph.actor,
        )
    response = setup.portal().post(
        f"/solicitacoes/{setup.request.pk}/enviar",
        {"file": upload("nota.pdf", PAYLOAD)},
    )
    assert response.status_code == HTTPStatus.FOUND
    with tenant_context(setup.graph.tenant.pk):
        link = EvidenceLink.objects.get()
        assert link.document_id != foreign.pk
        assert link.relinked is False
        assert link.document.client_id == setup.graph.client.pk
