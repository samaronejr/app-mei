"""The capture records obey the existing tenant/client database contract."""

from __future__ import annotations

import importlib
import importlib.util
from typing import TYPE_CHECKING

import pytest
from django.db import IntegrityError, transaction
from django.db.models import CharField, UUIDField

from apps.core.models import TenantScopedModel
from apps.core.tenancy import tenant_context
from tests.conftest import PORTAL_DENIED_TABLES
from tests.cycles.fiscal_review_fixtures import constraint_name, insert_review_edge
from tests.cycles.test_fiscal_document_review import record, world
from tests.integrations.capture_fixtures import model

if TYPE_CHECKING:
    from django.db.models import Model

    from tests.integrations.capture_fixtures import CaptureWorld

__all__ = ["world"]


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


@pytest.mark.django_db(transaction=True)
def test_fiscal_review_has_one_root_and_head_per_exact_revision_and_cycle(
    world: CaptureWorld,
) -> None:
    root = record(world, world.ingest(), "rejeitado")
    with tenant_context(world.tenant_id):
        successor = insert_review_edge(root, root.pk)
        for predecessor in (None, root.pk):
            with pytest.raises(IntegrityError) as refused, transaction.atomic():
                insert_review_edge(root, predecessor)
            constraint = constraint_name(refused.value)
            assert constraint == "fiscaldocumentreview_revision_uniq"
        reviews = type(root).objects.filter(
            tenant_id=root.tenant_id,
            client_id=root.client_id,
            revision_id=root.revision_id,
            cycle_id=root.cycle_id,
        )
        chain = dict(reviews.values_list("pk", "supersedes_id"))
    assert chain == {root.pk: None, successor: root.pk}
    assert set(chain).difference(chain.values()) == {successor}


def test_fiscal_review_decision_choices_are_unchanged() -> None:
    decision = model("FiscalDocumentReview", app="cycles")._meta.get_field("decision")
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
