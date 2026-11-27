"""The staff review cannot expose a superseded head's completion receipt."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

import pytest
from django.db import connection
from pytest_django.fixtures import SettingsWrapper

from apps.cycles import fiscal_views
from apps.obligations.iss_retention_readiness import iss_party_is_ready
from apps.obligations.models import FiscalDocument
from tests.obligations import test_fiscal_documents
from tests.obligations.fiscal_fixtures import retention_element
from tests.obligations.iss_backfill_fixtures import _ingest_retention_note, _payload
from tests.obligations.iss_writer_fixtures import post_scan_503
from tests.obligations.test_fiscal_documents import document
from tests.obligations.test_iss_backfill_review import _assert_pending, _review_body
from tests.portal.request_fixtures import PortalSetup

pytestmark = pytest.mark.django_db(transaction=True)
setup = test_fiscal_documents.setup


def test_iss_review_fails_closed_when_head_advances_during_get(
    setup: PortalSetup, settings: SettingsWrapper
) -> None:
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    browser = setup.firm()
    first = _ingest_retention_note(setup, "2")
    assert "ISS retido pelo tomador" in _review_body(browser, first.pk)
    head_loaded = Event()

    def advance() -> tuple[FiscalDocument, int]:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET ROLE app_runtime")
                cursor.execute("SELECT pg_backend_pid()")
                (writer_pid,) = cursor.fetchone()
            assert head_loaded.wait(timeout=30), "The GET did not reach readiness"
            with post_scan_503(_payload(retention_element("3"))):
                latest = _ingest_retention_note(setup, "3", version=2)
            # Ingest commits the new head before this worker can return.
            assert connection.get_autocommit()
            return latest, writer_pid
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        advancing = pool.submit(advance)

        def advance_before_check(loaded: FiscalDocument) -> bool:
            assert loaded.current_revision_id == first.current_revision_id
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                (reader_pid,) = cursor.fetchone()
            head_loaded.set()
            latest, writer_pid = advancing.result(timeout=30)
            assert reader_pid != writer_pid
            assert latest.pk == loaded.pk
            assert latest.current_revision_id != loaded.current_revision_id
            assert latest.fiscal_state == "incerta"
            assert latest.iss_retention_party == "desconhecido"
            current = document(setup)
            assert current.current_revision_id == latest.current_revision_id
            return iss_party_is_ready(loaded)

        try:
            with patch.object(
                fiscal_views, "iss_party_is_ready", side_effect=advance_before_check
            ):
                overlapping = _review_body(browser, first.pk)
        finally:
            head_loaded.set()

    _assert_pending(_review_body(browser, first.pk))  # A fresh GET sees the outage.
    _assert_pending(overlapping)
