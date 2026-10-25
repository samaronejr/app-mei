"""A report's private original cannot race a visibility update in either order."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from django.db import DatabaseError, connection, transaction

from apps.core.tenancy import tenant_context
from apps.obligations.federal import record_payment_consultation
from apps.obligations.models import Document, FiscalSituationReport
from tests.obligations.test_federal_records import (
    _finalized_call,
    _report_document,
    federal_case,
)
from tests.portal.request_fixtures import PortalSetup
from tests.support_threads import run_in_thread_as_runtime

__all__ = ["federal_case"]
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("first", ["attachment", "visibility"])
@pytest.mark.parametrize("isolation", ["READ COMMITTED", "REPEATABLE READ"])
@pytest.mark.parametrize("reassignment", [False, True])
def test_report_privacy_survives_overlapping_transactions(
    federal_case: PortalSetup, first: str, isolation: str, reassignment: bool
) -> None:
    # Given an unbound report and private document in committed rows.
    graph = federal_case.graph
    with tenant_context(graph.tenant.pk):
        receipt = record_payment_consultation(
            graph.client, _finalized_call(graph.client, "fake")
        )
        document = _report_document(federal_case, visible=False)
        previous = (
            Document.objects.create(
                tenant=graph.tenant,
                client=graph.client,
                sha256="e" * 64,
                original_filename="previous-report.pdf",
                content_type="application/pdf",
                byte_size=1,
                uploaded_by=graph.actor,
                client_upload=False,
                portal_visible=False,
                acquisition_origin="upload_equipe",
            )
            if reassignment
            else None
        )
        report = FiscalSituationReport.objects.create(
            tenant=graph.tenant,
            client=graph.client,
            consultation=receipt,
            requested_at=receipt.consulted_at,
            document=previous,
        )
    held, release = Event(), Event()

    def first_writer() -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles "
                "WHERE rolname = current_user"
            )
            assert cursor.fetchone() == ("app_runtime", False, False)
        if first == "attachment":
            FiscalSituationReport.objects.filter(pk=report.pk).update(document=document)
        else:
            Document.objects.filter(pk=document.pk).update(portal_visible=True)
        held.set()
        assert release.wait(timeout=30), "The first transaction was not released."

    def second_writer() -> None:
        if first == "attachment":
            Document.objects.filter(pk=document.pk).update(portal_visible=True)
        else:
            FiscalSituationReport.objects.filter(pk=report.pk).update(document=document)

    # When the second transaction takes its snapshot before the first commits.
    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(run_in_thread_as_runtime, graph.tenant.pk, first_writer)
        try:
            assert held.wait(timeout=30), "The first writer did not reach its write."
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(f"SET TRANSACTION ISOLATION LEVEL {isolation}")
                with tenant_context(graph.tenant.pk):
                    # Establish a repeatable-read snapshot while the first write
                    # remains uncommitted. No timing-based wait/poll is involved.
                    assert FiscalSituationReport.objects.get(
                        pk=report.pk
                    ).document_id == (previous.pk if previous is not None else None)
                    release.set()
                    with (
                        pytest.raises(
                            DatabaseError,
                            match=r"sitfisreport_document_private_fk|firm-only"
                            r"|could not serialize",
                        ),
                        transaction.atomic(),
                    ):
                        second_writer()
        finally:
            release.set()
        writer.result(timeout=30)

    # Then exactly the first write won; no report references a visible document.
    with tenant_context(graph.tenant.pk):
        report.refresh_from_db()
        document.refresh_from_db()
        assert (report.document_id, document.portal_visible) == (
            (document.pk, False)
            if first == "attachment"
            else (previous.pk if previous is not None else None, True)
        )
