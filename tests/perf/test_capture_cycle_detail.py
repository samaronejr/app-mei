"""The existing cycle-detail budget includes 300 captured fiscal identities."""

from __future__ import annotations

import gzip
import json
import sys
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.core.tenancy import tenant_context
from apps.cycles.scanning import FakeScanner
from apps.obligations.models import FiscalDocument, FiscalRevision, RevisionArtifact
from ops.perf.results import GZIP_BYTES, QUERIES
from tests.integrations.capture_fixtures import capture_world, key

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_django.fixtures import DjangoAssertNumQueries, SettingsWrapper

pytestmark = pytest.mark.django_db(transaction=True)


def test_cycle_detail_with_three_hundred_captured_documents_stays_within_budget(
    settings: SettingsWrapper,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    django_assert_max_num_queries: DjangoAssertNumQueries,
) -> None:
    world = capture_world(settings, tmp_path, monkeypatch)
    with patch(
        "apps.obligations.fiscal_ingest.scanner_from_settings",
        return_value=FakeScanner(),
    ):
        first = world.ingest()
    with tenant_context(world.tenant_id):
        retained = RevisionArtifact.objects.get(
            revision_id=first.current_revision_id, role="xml_original"
        )
        documents = FiscalDocument.objects.bulk_create(
            [
                FiscalDocument(
                    tenant_id=world.tenant_id,
                    client_id=world.link.client_id,
                    environment="fake",
                    family="nfe",
                    key_namespace="sefaz",
                    document_key=key(index),
                    client_role="destinatario",
                    counterpart_name="Synthetic issuer",
                    issue_date=first.issue_date,
                    total=first.total,
                    summary_only=False,
                    source="captura",
                    provider="focus",
                    fiscal_state="autorizada",
                    role_verification="confirmed",
                )
                for index in range(2, 301)
            ]
        )
        revisions = FiscalRevision.objects.bulk_create(
            [
                FiscalRevision(
                    tenant_id=world.tenant_id,
                    client_id=world.link.client_id,
                    document=document,
                    source_namespace="focus:nfe_recebidas",
                    provider_version=str(index),
                    semantic_digest=f"{index:064x}",
                    fiscal_state="autorizada",
                    receipt_ref="synthetic-budget-page",
                )
                for index, document in enumerate(documents, 2)
            ]
        )
        RevisionArtifact.objects.bulk_create(
            [
                RevisionArtifact(
                    tenant_id=world.tenant_id,
                    client_id=world.link.client_id,
                    revision=revision,
                    role="xml_original",
                    document_id=retained.document_id,
                )
                for revision in revisions
            ]
        )
        for document, revision in zip(documents, revisions, strict=True):
            document.current_revision = revision
        FiscalDocument.objects.bulk_update(documents, ["current_revision"])
        assert FiscalDocument.objects.count() == 300
    browser = world.setup.firm()
    with django_assert_max_num_queries(QUERIES) as queries:
        response = browser.get(reverse("cycle-detail", args=[world.setup.cycle.pk]))
    assert response.status_code == 200
    assert response.content.count(b"data-fiscal-document=") == 300
    size = len(gzip.compress(response.content, mtime=0))
    sys.stdout.write(
        "\nAXRM_CAPTURE_READ="
        + json.dumps(
            {"documents": 300, "queries": len(queries), "gzip_html_bytes": size},
            sort_keys=True,
        )
        + "\n"
    )
    assert size <= GZIP_BYTES
