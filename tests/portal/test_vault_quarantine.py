"""T13 vault quarantine (plan C-09): the portal vault upload no longer bypasses T12.

Before T13 `document_upload` wrote straight to the final key with no sniffing and no
intake, and the serve rule only hid documents that HAD an intake -- so a vault upload
was downloadable at once by every portal account of the client. Now the vault goes
through sniff -> quarantine -> intake, and the portal serves a client upload only
through a verified intake (fail closed). A firm upload keeps its behaviour.
"""

import hashlib
import importlib
from http import HTTPStatus
from pathlib import Path

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import connection, transaction
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.core import identifiers
from apps.core.tenancy import tenant_context
from apps.cycles.evidence import DocumentIntake, IntakeState, quarantine_key
from apps.cycles.scanning import FakeScanner, scan_intake
from apps.cycles.tasks import scan_quarantine
from apps.obligations.models import Document, new_storage_key
from apps.tenants.models import TenantRole
from tests.portal.request_fixtures import PortalSetup, portal_setup, upload
from tests.ui.factories import add_member

DB = pytest.mark.django_db(transaction=True)
PDF_BYTES = b"%PDF-1.4\n% extrato sintetico do cofre\n%%EOF\n"
HTML_BYTES = b"<!doctype html><script>alert(1)</script>"


@pytest.fixture
def setup(settings: SettingsWrapper, tmp_path: Path) -> PortalSetup:
    settings.MEDIA_ROOT = tmp_path
    settings.CLAMAV_ENABLED = False
    return portal_setup("t13-vault")


def _colleague(setup: PortalSetup) -> User:
    """A second portal account of the SAME client."""
    return add_member(
        setup.graph.tenant,
        "colega@t13-vault.test",
        TenantRole.CLIENT_COLLABORATOR,
        client=setup.graph.client,
    )


def _vault_post(setup: PortalSetup, name: str, payload: bytes) -> int:
    response = setup.portal().post(
        "/documentos/enviar", {"file": upload(name, payload, "application/pdf")}
    )
    return int(response.status_code)


def _vault_document(setup: PortalSetup, name: str) -> Document:
    with tenant_context(setup.graph.tenant.pk):
        return Document.objects.get(original_filename=name)


def _intake(setup: PortalSetup, document: Document) -> DocumentIntake:
    with tenant_context(setup.graph.tenant.pk):
        return DocumentIntake.objects.get(document=document)


def _stored(setup: PortalSetup, uploader: User, *, client_upload: bool) -> Document:
    """A document row with bytes at its final key and NO intake (pre-T13 shape)."""
    payload = f"sem intake {client_upload}".encode()
    document = Document(
        tenant=setup.graph.tenant,
        client=setup.graph.client,
        sha256=hashlib.sha256(payload).hexdigest(),
        original_filename=f"legado-{client_upload}.pdf",
        content_type="application/pdf",
        byte_size=len(payload),
        uploaded_by=uploader,
        storage_key=new_storage_key(),
        client_upload=client_upload,
    )
    default_storage.save(document.storage_key, ContentFile(payload))
    with tenant_context(setup.graph.tenant.pk):
        document.save(force_insert=True)
    return document


@DB
def test_unscanned_vault_upload_is_not_downloadable_by_another_account(
    setup: PortalSetup,
) -> None:
    colleague = _colleague(setup)
    assert _vault_post(setup, "extrato.pdf", PDF_BYTES) == HTTPStatus.FOUND
    document = _vault_document(setup, "extrato.pdf")

    # Not served before a verdict -- to another account of the client, nor its uploader.
    for account in (colleague, setup.portal_user):
        portal = setup.portal(account)
        assert portal.get(f"/documentos/{document.storage_key}").status_code == 404
        assert "extrato.pdf" not in portal.get("/documentos/").content.decode()
    # Quarantined like a request upload: sniffed type, quarantine key, no final key.
    intake = _intake(setup, document)
    assert intake.state == IntakeState.QUARANTINED
    assert document.content_type == "application/pdf"
    assert intake.quarantine_key == quarantine_key(
        setup.graph.tenant.pk, setup.graph.client.pk, document.storage_key
    )
    assert not default_storage.exists(document.storage_key)

    # Scanned with the scanner off: still never served in the portal.
    scan_intake(setup.graph.tenant.pk, intake.pk, scanner=None, now=timezone.now())
    assert _intake(setup, document).state == IntakeState.SCAN_UNAVAILABLE
    colleague_portal = setup.portal(colleague)
    assert (
        colleague_portal.get(f"/documentos/{document.storage_key}").status_code == 404
    )


@DB
def test_vault_upload_is_served_once_the_scanner_says_clean(setup: PortalSetup) -> None:
    colleague = _colleague(setup)
    _vault_post(setup, "extrato.pdf", PDF_BYTES)
    document = _vault_document(setup, "extrato.pdf")

    scan_intake(
        setup.graph.tenant.pk,
        _intake(setup, document).pk,
        scanner=FakeScanner(),
        now=timezone.now(),
    )

    portal = setup.portal(colleague)
    response = portal.get(f"/documentos/{document.storage_key}")
    assert response.status_code == HTTPStatus.OK
    assert response.content == PDF_BYTES
    assert response["Content-Security-Policy"] == "sandbox"
    assert "extrato.pdf" in portal.get("/documentos/").content.decode()


@DB
def test_vault_refuses_a_disallowed_type_and_keeps_the_original_unserved(
    setup: PortalSetup,
) -> None:
    assert _vault_post(setup, "nota.pdf", HTML_BYTES) == HTTPStatus.BAD_REQUEST
    document = _vault_document(setup, "nota.pdf")
    intake = _intake(setup, document)

    assert intake.state == IntakeState.REJECTED_TYPE
    assert default_storage.exists(intake.quarantine_key)
    assert setup.portal().get(f"/documentos/{document.storage_key}").status_code == 404
    # The same bytes again are refused again.
    assert _vault_post(setup, "de-novo.pdf", HTML_BYTES) == HTTPStatus.BAD_REQUEST


@DB
def test_client_upload_without_any_intake_fails_closed(setup: PortalSetup) -> None:
    legacy = _stored(setup, setup.portal_user, client_upload=True)
    portal = setup.portal(_colleague(setup))

    assert portal.get(f"/documentos/{legacy.storage_key}").status_code == 404
    assert legacy.original_filename not in portal.get("/documentos/").content.decode()


@DB
def test_firm_uploaded_document_keeps_its_behaviour(setup: PortalSetup) -> None:
    firm_doc = _stored(setup, setup.graph.actor, client_upload=False)
    portal = setup.portal()

    # Served to the client's portal without an intake, as before T13...
    response = portal.get(f"/documentos/{firm_doc.storage_key}")
    assert response.status_code == HTTPStatus.OK
    assert firm_doc.original_filename in portal.get("/documentos/").content.decode()
    # ...and the firm's own download still shows T12's warning before any byte.
    staff = setup.firm().get(f"/documentos/{firm_doc.pk}/baixar/")
    assert staff.status_code == HTTPStatus.OK
    assert b"sem intake" not in staff.content


@DB
def test_portal_role_cannot_insert_a_firm_upload(setup: PortalSetup) -> None:
    def insert(*, client_upload: bool) -> None:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL ROLE app_portal")
            cursor.execute(
                "SELECT set_config('app.tenant_id', %s, true),"
                " set_config('app.client_id', %s, true)",
                [str(setup.graph.tenant.pk), str(setup.graph.client.pk)],
            )
            cursor.execute(
                """
                INSERT INTO obligations_document
                  (id, tenant_id, client_id, storage_key, sha256, original_filename,
                   content_type, byte_size, uploaded_by_id, created_at, client_upload)
                VALUES (%s, %s, %s, %s, %s, 'x.pdf', 'application/pdf', 1, %s, now(),
                        %s)
                """,
                [
                    str(identifiers.uuid7()),
                    str(setup.graph.tenant.pk),
                    str(setup.graph.client.pk),
                    new_storage_key(),
                    hashlib.sha256(str(client_upload).encode()).hexdigest(),
                    str(setup.portal_user.pk),
                    client_upload,
                ],
            )

    with pytest.raises(Exception) as refused, transaction.atomic():  # noqa: PT011
        insert(client_upload=False)
    assert "document_portal_client_upload" in str(refused.value)
    assert "permission denied" not in str(refused.value)
    # Positive control: the same INSERT claiming a client upload is accepted.
    with transaction.atomic():
        insert(client_upload=True)


@DB
def test_backfill_marks_only_client_members_uploads_as_client_uploads(
    setup: PortalSetup,
) -> None:
    by_client = _stored(setup, setup.portal_user, client_upload=False)
    by_firm = _stored(setup, setup.graph.actor, client_upload=True)

    backfill = importlib.import_module(
        "apps.obligations.migrations.0019_document_client_upload"
    ).BACKFILL
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
        cursor.execute(backfill)
        cursor.execute("SET ROLE app_runtime")

    with tenant_context(setup.graph.tenant.pk):
        assert Document.objects.get(pk=by_client.pk).client_upload is True
        assert Document.objects.get(pk=by_firm.pk).client_upload is False


@DB
def test_sweep_scans_vault_uploads_of_firms_without_the_flag(
    setup: PortalSetup,
) -> None:
    tenant = setup.graph.tenant
    tenant.features = []
    tenant.save(update_fields=["features"])
    assert _vault_post(setup, "extrato.pdf", PDF_BYTES) == HTTPStatus.FOUND
    document = _vault_document(setup, "extrato.pdf")

    assert scan_quarantine.apply().get() == 1
    assert _intake(setup, document).state == IntakeState.SCAN_UNAVAILABLE
