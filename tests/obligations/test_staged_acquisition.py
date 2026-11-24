"""Initial quarantine is outside registration, even for raw driver transactions."""

from __future__ import annotations

import hashlib
from contextlib import nullcontext
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, transaction

from apps.core.tasks import assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.cycles import intake
from apps.obligations.models import Document, FiscalDocument
from tests.obligations import test_fiscal_documents as direct
from tests.obligations.fiscal_fixtures import nfse

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.portal.request_fixtures import PortalSetup

pytestmark = pytest.mark.django_db(transaction=True)
setup = direct.setup


@pytest.mark.parametrize("source", ["importacao_equipe", "upload_cliente", "captura"])
def test_initial_quarantine_only_on_idle_connection(
    setup: PortalSetup, source: str
) -> None:
    actual_store: Callable[..., object] = intake._store
    calls: list[bool] = []

    def observe(*args: object, **kwargs: object) -> object:
        try:
            assert_no_transaction()
        except RuntimeError:
            calls.append(False)
        else:
            calls.append(True)
        return actual_store(*args, **kwargs)

    with patch.object(intake, "_store", side_effect=observe):
        result = direct.ingest(
            setup, source=source, payload=nfse(), client_role="tomador"
        )
    assert isinstance(result, FiscalDocument)
    assert calls == [True], (
        "New quarantine bytes were stored inside a database transaction."
    )


@pytest.mark.parametrize("mode", ["atomic", "raw-begin"])
def test_store_itself_refuses_before_storage(setup: PortalSetup, mode: str) -> None:
    payload = nfse()
    upload = SimpleUploadedFile("invoice.xml", payload)
    accessed = AssertionError("unexpected storage access")
    refusal = ""
    with (
        patch.object(default_storage, "save", side_effect=accessed) as save,
        patch.object(default_storage, "exists", side_effect=accessed) as exists,
        transaction.atomic() if mode == "atomic" else nullcontext(),
    ):
        if mode == "raw-begin":
            with connection.cursor() as cursor:
                cursor.execute("BEGIN")
        try:
            try:
                intake._store(
                    tenant=setup.graph.tenant,
                    client=setup.graph.client,
                    source=intake.DocumentSource(actor=setup.portal_user),
                    upload=upload,
                    payload=payload,
                    digest=hashlib.sha256(payload).hexdigest(),
                )
            except RuntimeError as error:
                refusal = str(error)
            except AssertionError as error:
                if error is not accessed:
                    raise
        finally:
            if mode == "raw-begin":
                connection.rollback()
    assert "External I/O" in refusal, "_store did not refuse an open transaction."
    save.assert_not_called()
    exists.assert_not_called()
    with tenant_context(setup.graph.tenant.pk):
        assert not Document.objects.exists()
        assert not FiscalDocument.objects.exists()
