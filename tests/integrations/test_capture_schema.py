"""The capture records obey the existing tenant/client database contract."""

from __future__ import annotations

import importlib
import importlib.util
from typing import TYPE_CHECKING

import pytest
from django.db.models import CharField, UUIDField

from apps.core.models import TenantScopedModel
from tests.conftest import PORTAL_DENIED_TABLES
from tests.integrations.capture_fixtures import model

if TYPE_CHECKING:
    from django.db.models import Model


@pytest.mark.parametrize(
    ("app", "name"),
    [
        ("integrations", "SyncStream"),
        ("integrations", "SyncRun"),
        ("integrations", "CaptureAcquisition"),
        ("cycles", "FiscalDocumentReview"),
    ],
)
def test_every_capture_record_is_registered_scoped_and_portal_denied(
    app: str, name: str
) -> None:
    record: type[Model] = model(name, app=app)
    assert issubclass(record, TenantScopedModel)
    assert isinstance(record._meta.pk, UUIDField)
    for field in ("tenant", "client"):
        assert record._meta.get_field(field).null is False
    assert record._meta.db_table in PORTAL_DENIED_TABLES
    for constraint in record._meta.constraints:
        fields = getattr(constraint, "fields", ())
        if fields:
            assert "tenant" in fields
            assert "client" in fields


def test_stream_identity_and_run_occurrence_have_exact_client_scoped_uniques() -> None:
    stream = model("SyncStream")
    run = model("SyncRun")
    assert any(
        tuple(getattr(constraint, "fields", ()))
        == ("tenant", "client", "link", "collection", "filter_digest", "epoch")
        for constraint in stream._meta.constraints
    )
    assert any(
        tuple(getattr(constraint, "fields", ()))
        == ("tenant", "client", "stream", "occurrence_key")
        for constraint in run._meta.constraints
    )
    assert {
        "start_cursor",
        "end_cursor",
        "pages_committed",
        "requests_made",
        "documents_accounted",
        "page_limit",
        "request_limit",
    } <= {field.name for field in run._meta.fields}


def test_fiscal_review_has_one_decision_per_exact_revision_and_cycle() -> None:
    review = model("FiscalDocumentReview", app="cycles")
    assert any(
        tuple(getattr(constraint, "fields", ()))
        == ("tenant", "client", "revision", "cycle")
        for constraint in review._meta.constraints
    )
    decision = review._meta.get_field("decision")
    assert isinstance(decision, CharField)
    assert decision.choices is not None
    assert {value for value, _label in decision.choices} == {
        "aceito",
        "rejeitado",
        "requer_xml",
        "documento_de_terceiro",
    }


@pytest.mark.parametrize(
    "module",
    [
        "apps.integrations.migrations.0016_capture_sync",
        "apps.cycles.migrations.0023_fiscal_document_review",
    ],
)
def test_capture_migrations_explicitly_guard_reversal_after_real_rows_exist(
    module: str,
) -> None:
    assert importlib.util.find_spec(module) is not None, (
        "Capture and professional review records need their scoped migration."
    )
    migration = importlib.import_module(module)
    assert callable(migration.refuse_reverse_with_rows)
