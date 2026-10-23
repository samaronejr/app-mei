"""INV-R10: hidden same-client rows stay hidden even after a clean scan."""

import hashlib
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import ProgrammingError, connection, transaction
from psycopg import sql
from pytest_django.fixtures import SettingsWrapper

from apps.core.identifiers import uuid7
from apps.core.tenancy import tenant_context
from apps.cycles.evidence import DocumentIntake, IntakeState
from apps.obligations.models import Document, Obligation
from apps.portal.views import _next_action, _situation
from tests.obligations.ingest_fixtures import PDF, ingest_setup, portal_cursor
from tests.portal.request_fixtures import PortalSetup, upload

if TYPE_CHECKING:
    from uuid import UUID

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def setup(settings: SettingsWrapper, tmp_path: Path) -> PortalSetup:
    return ingest_setup(settings, tmp_path)


def _document(setup: PortalSetup, *, visible: bool) -> Document:
    payload = PDF if not visible else PDF + b"client upload"
    with tenant_context(setup.graph.tenant.pk):
        document = Document.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            sha256=hashlib.sha256(payload).hexdigest(),
            original_filename=f"visible-{visible}.pdf",
            content_type="application/pdf",
            byte_size=len(payload),
            uploaded_by=setup.portal_user,
            portal_visible=visible,
        )
        DocumentIntake.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            document=document,
            state=IntakeState.VERIFIED,
        )
    default_storage.save(document.storage_key, ContentFile(payload))
    return document


def _obligation(setup: PortalSetup, *, visible: bool) -> Obligation:
    with tenant_context(setup.graph.tenant.pk):
        return Obligation.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            obligation_type_id="DAS",
            competence_month=date(2030, 2 if visible else 1, 1),
            nominal_due_date=date(2030, 3 if visible else 2, 20),
            resolved_due_date=date(2030, 3 if visible else 2, 20),
            portal_visible=visible,
        )


@pytest.mark.parametrize("table", ["obligations_document", "obligations_obligation"])
def test_portal_select_hides_same_client_rows_with_bypass_positive_control(
    setup: PortalSetup, table: str
) -> None:
    # Given real hidden/visible rows, not an empty table or a missing grant.
    factory = _document if table == "obligations_document" else _obligation
    hidden = factory(setup, visible=False)
    visible = factory(setup, visible=True)
    query = sql.SQL("SELECT id FROM {} WHERE id IN (%s, %s)").format(
        sql.Identifier(table)
    )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
        cursor.execute(
            "SELECT current_user, rolsuper, rolbypassrls "
            "FROM pg_roles WHERE rolname = current_user"
        )
        assert cursor.fetchone() == ("app_test", False, True)
        cursor.execute(query, [hidden.pk, visible.pk])
        assert {row[0] for row in cursor.fetchall()} == {hidden.pk, visible.pk}

    # When the same rows are read under the actual portal role.
    with portal_cursor(setup) as cursor:
        cursor.execute(query, [hidden.pk, visible.pk])
        # Then only the explicitly visible row survives the policy.
        assert cursor.fetchall() == [(visible.pk,)]


def test_clean_hidden_document_is_absent_from_vault_and_download(
    setup: PortalSetup,
) -> None:
    # Given scanner-cleared documents, so quarantine cannot mask a missing policy.
    hidden = _document(setup, visible=False)
    visible = _document(setup, visible=True)
    portal = setup.portal()
    # When the real middleware and views read them.
    page = portal.get("/documentos/")
    # Then the legacy client upload works and the hidden original cannot be served.
    assert page.status_code == 200
    assert visible.original_filename in page.content.decode()
    assert hidden.original_filename not in page.content.decode()
    assert (
        portal.get(f"/documentos/{visible.storage_key}").content
        == PDF + b"client upload"
    )
    assert portal.get(f"/documentos/{hidden.storage_key}").status_code == 404


def test_hidden_obligation_is_absent_from_payments_totals_and_next_action(
    setup: PortalSetup,
) -> None:
    # Given a hidden earlier-due row and a visible future row.
    _obligation(setup, visible=False)
    visible = _obligation(setup, visible=True)
    # When the real payments page reads them.
    response = setup.portal().get("/pagamentos/")
    # Then only the visible period reaches the page, totals and next action.
    assert response.status_code == 200
    assert [payment.competence_month for payment in response.context["payments"]] == [
        visible.competence_month
    ]
    with portal_cursor(setup):
        assert _situation(date(2030, 3, 1)) == (1, 0)
        action = _next_action(date(2030, 1, 1))
        assert action is not None
        assert action.competence_month == visible.competence_month


def test_same_bytes_client_upload_creates_a_separate_visible_document(
    setup: PortalSetup,
) -> None:
    # Given bytes already held as an invisible original.
    hidden = _document(setup, visible=False)
    # When the client uploads those exact bytes through the portal.
    response = setup.portal().post(
        "/documentos/enviar", {"file": upload("client.pdf", PDF)}
    )
    # Then it neither collides nor reuses the hidden row.
    assert response.status_code == 302
    with tenant_context(setup.graph.tenant.pk):
        visible = Document.objects.get(sha256=hidden.sha256, portal_visible=True)
        assert visible.pk != hidden.pk
        assert visible.uploaded_by_id == setup.portal_user.pk
        assert visible.client_upload is True
        assert visible.acquisition_origin == "upload_portal"
        assert visible.acquisition_receipt_ref == ""


@pytest.mark.parametrize(
    ("visible", "origin", "human", "receipt"),
    [
        (False, "upload_portal", True, ""),
        (True, "upload_equipe", True, ""),
        (True, "captura_provedor", True, ""),
        (True, "provedor_federal", True, ""),
        (True, "upload_portal", False, ""),
        (True, "upload_portal", True, "forged-receipt"),
    ],
)
def test_portal_insert_cannot_forge_visibility_or_provenance(
    setup: PortalSetup, visible: bool, origin: str, human: bool, receipt: str
) -> None:
    # Given a real portal role with INSERT privileges.
    with (
        pytest.raises(
            ProgrammingError, match="document_portal_client_upload"
        ) as refused,
        portal_cursor(setup) as cursor,
    ):
        # When it tries to mint staff/provider metadata or a hidden row.
        cursor.execute(
            "INSERT INTO obligations_document "
            "(id, tenant_id, client_id, storage_key, sha256, original_filename, "
            "content_type, byte_size, uploaded_by_id, created_at, portal_visible, "
            "acquisition_origin, acquisition_receipt_ref) "
            "VALUES (%s, %s, %s, %s, %s, 'x.pdf', 'application/pdf', 1, %s, "
            "now(), %s, %s, %s)",
            [
                uuid7(),
                setup.graph.tenant.pk,
                setup.graph.client.pk,
                str(uuid7()),
                "a" * 64,
                setup.portal_user.pk if human else None,
                visible,
                origin,
                receipt,
            ],
        )
    # Then the trigger, rather than a missing privilege, refuses it.
    assert getattr(refused.value.__cause__, "sqlstate", None) == "42501"
    assert "permission denied" not in str(refused.value)


@pytest.mark.parametrize("table", ["obligations_document", "obligations_obligation"])
def test_with_check_refuses_hidden_insert_independently_of_the_trigger(
    setup: PortalSetup, table: str
) -> None:
    # Given a rolled-back owner transaction: isolate WITH CHECK from earlier guards.
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
        cursor.execute(
            "ALTER TABLE obligations_document "
            "DISABLE TRIGGER document_portal_client_upload"
        )
        cursor.execute("GRANT INSERT ON obligations_obligation TO app_portal")
        if table == "obligations_document":
            statement = (
                "INSERT INTO obligations_document "
                "(id, tenant_id, client_id, storage_key, sha256, original_filename, "
                "content_type, byte_size, uploaded_by_id, created_at, portal_visible) "
                "VALUES (%s, %s, %s, %s, %s, 'x.pdf', 'application/pdf', 1, "
                "%s, now(), false)"
            )
            params: list[UUID | str] = [
                uuid7(),
                setup.graph.tenant.pk,
                setup.graph.client.pk,
                str(uuid7()),
                "a" * 64,
                setup.portal_user.pk,
            ]
        else:
            statement = (
                "INSERT INTO obligations_obligation "
                "(id, tenant_id, client_id, obligation_type_id, competence_month, "
                "nominal_due_date, resolved_due_date, status, evidence_ref, "
                "created_at, updated_at, portal_visible) "
                "VALUES (%s, %s, %s, 'DAS', '2030-01-01', '2030-02-20', "
                "'2030-02-20', 'scheduled', '', now(), now(), false)"
            )
            params = [uuid7(), setup.graph.tenant.pk, setup.graph.client.pk]
        with (
            pytest.raises(ProgrammingError, match="row-level security policy"),
            transaction.atomic(),
            portal_cursor(setup) as portal,
        ):
            # When app_portal inserts a valid but hidden row.
            portal.execute(statement, params)
        # Then the named policy refused the row; no temporary grant/DDL survives.
        transaction.set_rollback(True)
