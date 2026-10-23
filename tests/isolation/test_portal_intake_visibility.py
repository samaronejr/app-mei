"""Quarantine metadata must inherit the original document's portal visibility."""

from pathlib import Path

import pytest
from django.db import ProgrammingError, connection, transaction
from pytest_django.fixtures import SettingsWrapper

from apps.core.identifiers import uuid7
from apps.core.retention import RetentionClass
from apps.core.tenancy import tenant_context
from apps.cycles.evidence import DocumentIntake
from apps.cycles.intake import staff_upload
from apps.obligations.models import Document
from apps.obligations.system_ingest import ingest_system_document
from tests.isolation.test_portal_visibility_predicate import _document
from tests.obligations.ingest_fixtures import PDF, ingest_setup, portal_cursor
from tests.portal.request_fixtures import PortalSetup, upload

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def setup(settings: SettingsWrapper, tmp_path: Path) -> PortalSetup:
    return ingest_setup(settings, tmp_path)


def test_hidden_originals_do_not_disclose_intake_metadata(setup: PortalSetup) -> None:
    # Given same-client clean documents and the metadata of both originals.
    hidden = _document(setup, visible=False)
    visible = _document(setup, visible=True)
    with tenant_context(setup.graph.tenant.pk):
        hidden_intake = DocumentIntake.objects.get(document=hidden)
        visible_intake = DocumentIntake.objects.get(document=visible)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
        query = "SELECT id FROM cycles_documentintake WHERE id IN (%s, %s)"
        ids = [hidden_intake.pk, visible_intake.pk]
        cursor.execute(query, ids)
        assert {row[0] for row in cursor.fetchall()} == set(ids)
        # When the real portal role reads metadata without joining documents.
        with portal_cursor(setup) as portal:
            portal.execute(query, ids)
            # Then it sees the client upload's metadata, never the hidden original.
            assert portal.fetchall() == [(visible_intake.pk,)]
        transaction.set_rollback(True)


@pytest.mark.parametrize("visible", [False, True])
def test_portal_reads_back_intake_metadata_only_for_a_visible_parent(
    setup: PortalSetup, visible: bool
) -> None:
    # Given a same-client parent without an intake and the ordinary portal INSERT grant.
    with tenant_context(setup.graph.tenant.pk):
        document = Document.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            uploaded_by=setup.portal_user,
            portal_visible=visible,
            sha256="a" * 64,
            original_filename="parent.pdf",
            content_type="application/pdf",
            byte_size=1,
        )
    intake_id = uuid7()
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
        # RETURNING, as the ORM may emit it: the new row must also pass USING.
        statement = (
            "INSERT INTO cycles_documentintake "
            "(id, tenant_id, client_id, document_id, state, reason, "
            "created_at, version) "
            "VALUES (%s, %s, %s, %s, 'quarentena', '', now(), 1) RETURNING id"
        )
        params = [intake_id, setup.graph.tenant.pk, setup.graph.client.pk, document.pk]
        # When the real portal role appends metadata and reads it back.
        if visible:
            with portal_cursor(setup) as portal:
                portal.execute(statement, params)
                # Then its own visible upload's metadata round-trips.
                assert portal.fetchall() == [(intake_id,)]
        else:
            # Then a hidden parent's metadata is refused rather than returned.
            with (
                pytest.raises(ProgrammingError, match="row-level security policy"),
                transaction.atomic(),
                portal_cursor(setup) as portal,
            ):
                portal.execute(statement, params)
        transaction.set_rollback(True)


def _assert_no_metadata(setup: PortalSetup, document: Document) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
        cursor.execute(
            "SELECT count(*) FROM cycles_documentintake WHERE document_id = %s",
            [document.pk],
        )
        assert cursor.fetchone() == (1,)
        with portal_cursor(setup) as portal:
            portal.execute(
                "SELECT id, document_id, quarantine_key, state, reason "
                "FROM cycles_documentintake WHERE document_id = %s",
                [document.pk],
            )
            assert portal.fetchall() == []
            portal.execute(
                "SELECT count(*), min(quarantine_key), "
                "bool_or(state = 'verificado') FROM cycles_documentintake "
                "WHERE document_id = %s",
                [document.pk],
            )
            assert portal.fetchone() == (0, None, None)
        transaction.set_rollback(True)


@pytest.mark.parametrize("origin", ["provedor_federal", "captura_provedor"])
def test_actorless_ingest_exposes_neither_intake_metadata_nor_aggregates(
    setup: PortalSetup, origin: str
) -> None:
    # Given the real worker entry point, not a fabricated portal-shaped document.
    document = ingest_system_document(
        tenant_id=setup.graph.tenant.pk,
        client_id=setup.graph.client.pk,
        origin=origin,
        receipt_ref="metadata-isolation",
        payload=PDF,
        filename="provider.pdf",
        content_type="application/pdf",
        retention_class=RetentionClass.FISCAL_EVIDENCE,
    )
    # When the real portal role probes rows and aggregate metadata.
    # Then the owner sees its intake while the portal learns nothing about it.
    _assert_no_metadata(setup, document)


def test_other_clients_visible_intake_exposes_no_metadata(setup: PortalSetup) -> None:
    # Given a visible original for a sibling client of the same firm.
    with tenant_context(setup.graph.tenant.pk):
        document, _ = staff_upload(
            setup.graph.actor,
            upload("other-client.pdf", PDF),
            tenant=setup.graph.tenant,
            client=setup.other_client,
            portal_visible=True,
        )
    # When this client's actual portal role probes that known document ID.
    # Then row reads and aggregate probes both retain the client boundary.
    _assert_no_metadata(setup, document)
