"""Archive expiry removes disposable bytes, and read authority precedes storage."""

import hashlib
from collections.abc import Iterator
from datetime import timedelta
from http import HTTPStatus
from pathlib import Path
from typing import NoReturn

import pytest
from django.core.exceptions import ValidationError
from django.core.files import File
from django.core.files.storage import default_storage
from django.urls import reverse
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.archive_response import EvidenceArchiveResponse
from apps.cycles.export_records import EvidenceExport, ExportState
from apps.cycles.export_service import request_export
from apps.cycles.export_tasks import build_export
from apps.cycles.retention import place_hold
from apps.cycles.retention_tasks import purge_tenant
from apps.obligations.models import Document
from apps.tenants.models import Membership
from tests.cycles.document_fixtures import reviewed
from tests.portal.request_fixtures import PortalSetup, portal_setup
from tests.ui.refusal_contract import assert_readable_page, assert_readable_refusal

pytestmark = pytest.mark.django_db(transaction=True)


def ready_export(
    settings: SettingsWrapper,
    tmp_path: Path,
    slug: str,
) -> tuple[PortalSetup, EvidenceExport, Document]:
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    settings.MEDIA_ROOT = tmp_path
    setup = portal_setup(slug)
    intake, _ = reviewed(setup, "nfe_emitente_cliente.xml")
    with tenant_context(setup.graph.tenant.pk):
        setup.cycle.refresh_from_db()
        export = request_export(
            setup.graph.actor, setup.cycle.pk, expected_version=setup.cycle.version
        )
    build_export(str(setup.graph.tenant.pk), str(export.pk))
    with tenant_context(setup.graph.tenant.pk):
        export.refresh_from_db()
        assert export.state == ExportState.READY
    return setup, export, intake.document


@pytest.mark.parametrize("held", [False, True])
def test_thirty_day_expiry_never_deletes_originals_and_honors_hold(
    settings: SettingsWrapper,
    tmp_path: Path,
    held: bool,
) -> None:
    setup, export, document = ready_export(settings, tmp_path, "export-expiry")
    assert export.completed_at is not None
    assert export.expires_at is not None
    assert export.expires_at - export.completed_at == timedelta(days=30)
    with tenant_context(setup.graph.tenant.pk):
        if held:
            setup.graph.client.refresh_from_db()
            place_hold(
                setup.graph.actor,
                setup.graph.client.pk,
                reason="Preservar a cópia",
                expected_version=setup.graph.client.version,
            )
    counts = purge_tenant(
        setup.graph.tenant.pk,
        dry_run=False,
        now=export.expires_at + timedelta(seconds=1),
    )
    assert counts["purged"] == (0 if held else 1)
    assert default_storage.exists(export.storage_key) is held
    assert default_storage.exists(document.storage_key)
    with tenant_context(setup.graph.tenant.pk):
        export.refresh_from_db()
        assert export.state == (ExportState.READY if held else ExportState.EXPIRED)
        assert Document.objects.get(pk=document.pk).purged_at is None


def test_download_rechecks_revocation_and_foreign_scope_before_storage(
    settings: SettingsWrapper,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup, export, _ = ready_export(settings, tmp_path, "download-authority")
    foreign, other, _ = ready_export(settings, tmp_path, "download-foreign")
    browser = setup.firm()
    response = browser.get(f"/evidencias/arquivos/{export.pk}/baixar/")
    assert response.status_code == 200
    assert isinstance(response, EvidenceArchiveResponse)
    stream = response.streaming_content
    assert isinstance(stream, Iterator)
    body = b"".join(stream)
    response.close()
    assert hashlib.sha256(body).hexdigest() == export.sha256

    def forbid_open(*args: object, **kwargs: object) -> File[bytes]:
        pytest.fail("Denied download reached storage.")

    monkeypatch.setattr(default_storage, "open", forbid_open)
    assert browser.get(f"/evidencias/arquivos/{other.pk}/baixar/").status_code == 404
    with tenant_context(setup.graph.tenant.pk):
        Membership.objects.filter(
            tenant=setup.graph.tenant, user=setup.graph.actor
        ).update(is_active=False)
    assert browser.get(f"/evidencias/arquivos/{export.pk}/baixar/").status_code == 403
    with tenant_context(foreign.graph.tenant.pk):
        assert EvidenceExport.objects.get(pk=other.pk).state == ExportState.READY


@pytest.mark.parametrize(
    ("expired", "status", "reason"),
    [
        (True, HTTPStatus.GONE, "O prazo deste download encerrou."),
        (False, HTTPStatus.CONFLICT, "O arquivo ainda não está disponível."),
    ],
    ids=["prazo-encerrado", "ainda-em-preparo"],
)
def test_a_refused_download_is_a_readable_page_with_a_way_back(
    settings: SettingsWrapper,
    tmp_path: Path,
    expired: bool,
    status: HTTPStatus,
    reason: str,
) -> None:
    """AXR1-054: an expired or unfinished archive keeps its status and reads."""
    setup, export, _ = ready_export(
        settings, tmp_path, f"download-refusal-{int(expired)}"
    )
    with tenant_context(setup.graph.tenant.pk):
        rows = EvidenceExport.objects.filter(pk=export.pk)
        if expired:
            rows.update(expires_at=timezone.now() - timedelta(seconds=1))
        else:
            rows.update(state=ExportState.BUILDING)
    response = setup.firm().get(reverse("evidence-export-download", args=[export.pk]))
    assert_readable_refusal(
        response,
        status=status,
        reason=reason,
        back=reverse("evidence-export-detail", args=[export.pk]),
    )


def test_an_index_refusal_reads_in_plain_sentences(
    settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AXR1-054: a ValidationError from the index command reads as sentences.

    No service behind this form raises one today (by reading), so the command is
    replaced by one that does; what is pinned is the view's handling of it.
    """
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    setup = portal_setup("index-refusal")

    def refuse(*_args: object, **_kwargs: object) -> NoReturn:
        messages = [
            "O índice não pôde ser gerado.",
            "Confira as evidências e tente de novo.",
        ]
        raise ValidationError(messages)

    monkeypatch.setattr("apps.cycles.export_views.capture_index", refuse)
    with tenant_context(setup.graph.tenant.pk):
        setup.cycle.refresh_from_db()
    response = setup.firm().post(
        reverse("evidence-index", args=[setup.cycle.pk]),
        {"expected_version": str(setup.cycle.version), "action": "index"},
    )
    assert_readable_page(
        response,
        status=HTTPStatus.UNPROCESSABLE_CONTENT,
        reason="O índice não pôde ser gerado. Confira as evidências e tente de novo.",
    )
