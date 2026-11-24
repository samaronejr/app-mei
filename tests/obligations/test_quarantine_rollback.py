"""A quarantined object outliving its outer registration is safe to recover."""

from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.files.storage import default_storage
from django.core.management import call_command
from pytest_django.fixtures import SettingsWrapper

from apps.core.retention import RetentionClass
from apps.core.tasks import assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.cycles.evidence import DocumentIntake
from apps.obligations.models import AcquisitionOrigin, Document
from apps.obligations.system_ingest import ingest_system_document
from tests.obligations.ingest_fixtures import PDF, ingest_setup

pytestmark = pytest.mark.django_db(transaction=True)


def test_outer_rollback_leaves_reported_unserved_bytes_and_retry_reuses_them(
    settings: SettingsWrapper, tmp_path: Path
) -> None:
    setup = ingest_setup(settings, tmp_path)
    tenant, client = setup.graph.tenant, setup.graph.client

    def stop(_: Document) -> None:
        message = "caller registration rolled back"
        raise ValueError(message)

    def register(payload: bytes, *, rollback: bool = False) -> Document:
        return ingest_system_document(
            tenant_id=tenant.pk,
            client_id=client.pk,
            origin=AcquisitionOrigin.PROVIDER_CAPTURE,
            receipt_ref="proof",
            payload=payload,
            filename="input.pdf",
            content_type="application/pdf",
            retention_class=RetentionClass.FISCAL_EVIDENCE,
            on_registered=stop if rollback else None,
        )

    with (
        patch.object(default_storage, "save", wraps=default_storage.save) as save,
        pytest.raises(ValueError, match="caller registration rolled back"),
    ):
        register(PDF, rollback=True)
    assert save.call_count == 1
    held = save.call_args.args[0]
    with tenant_context(tenant.pk):
        assert not Document.objects.exists()
        assert not DocumentIntake.objects.filter(quarantine_key=held).exists()
    assert_no_transaction()
    assert default_storage.exists(held)
    report = StringIO()
    call_command("reconcile_documents", hash_sample_size=0, stdout=report)
    assert f"ORPHAN storage_key={held} " in report.getvalue()
    # An already-present object with the right length but different content
    # cannot be registered or destroyed by the next uploader.
    with default_storage.open(held, "wb") as damaged:
        damaged.write(b"!" + PDF[1:])
    with (
        patch.object(default_storage, "save", wraps=default_storage.save) as overwrite,
        pytest.raises(RuntimeError, match="integrity"),
    ):
        register(PDF)
    overwrite.assert_not_called()
    assert default_storage.exists(held)
    with tenant_context(tenant.pk):
        assert not Document.objects.exists()
        assert not DocumentIntake.objects.exists()
    with default_storage.open(held, "wb") as repaired:
        repaired.write(PDF)
    with patch.object(default_storage, "save", wraps=default_storage.save) as retry:
        document = register(PDF)
    retry.assert_not_called()
    with tenant_context(tenant.pk):
        assert DocumentIntake.objects.get(document=document).quarantine_key == held
        assert not document.portal_visible
    assert not default_storage.exists(document.storage_key)
