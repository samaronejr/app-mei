"""Pin exact scanned revisions without turning summaries into reviewed evidence."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, connection, transaction
from django.http import Http404
from django.urls import reverse

from apps.authz.objects import load_visible_client
from apps.briefing.providers import source_items
from apps.briefing.source_rows import SourceContext
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.cycles.errors import VersionConflict
from apps.cycles.models import ServiceCycle
from apps.cycles.records import CycleEvent
from apps.cycles.scanning import FakeScanner
from apps.obligations.models import RevisionArtifact
from tests.cycles.test_playbook_http_conflicts import HiddenInputs
from tests.integrations.capture_fixtures import (
    MONTH,
    START,
    capture_world,
    model,
    review_api,
)
from tests.obligations.fiscal_fixtures import summary

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from uuid import UUID

    from pytest_django.fixtures import SettingsWrapper

    from apps.accounts.models import User
    from apps.cycles.fiscal_records import FiscalDocumentReview
    from apps.obligations.models import FiscalDocument
    from tests.integrations.capture_fixtures import CaptureWorld

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def world(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[CaptureWorld]:
    graph = capture_world(settings, tmp_path, monkeypatch)
    with patch(
        "apps.obligations.fiscal_ingest.scanner_from_settings",
        return_value=FakeScanner(),
    ):
        yield graph


def record(
    world: CaptureWorld,
    fiscal: FiscalDocument,
    decision: str,
    **overrides: object,
) -> FiscalDocumentReview:
    api = review_api()
    with tenant_context(world.tenant_id):
        cycle = ServiceCycle.objects.get(pk=world.setup.cycle.pk)
        options = {
            "revision_id": fiscal.current_revision_id,
            "decision": decision,
            "input_digest": api.input_digest(fiscal),
            "expected_version": cycle.version,
            "note": "Synthetic internal review note",
            **overrides,
        }
        return cast(
            "FiscalDocumentReview",
            api.record_fiscal_document_review(
                world.setup.graph.actor, cycle.pk, fiscal.pk, **options
            ),
        )


@pytest.mark.parametrize(
    "decision", ["aceito", "rejeitado", "requer_xml", "documento_de_terceiro"]
)
def test_decisions_append_the_exact_revision_digest_reviewer_and_cycle_event(
    world: CaptureWorld, decision: str
) -> None:
    api = review_api()
    fiscal = world.ingest()
    with tenant_context(world.tenant_id):
        artifact = RevisionArtifact.objects.select_related("document").get(
            revision_id=fiscal.current_revision_id, role="xml_original"
        )
        expected = hashlib.sha256(f"1:{artifact.document.sha256}".encode()).hexdigest()
        material = api.input_digest(fiscal)
        assert material != expected
    review = record(world, fiscal, decision)
    assert review.decision == decision
    assert review.input_digest == material
    assert review.revision_id == fiscal.current_revision_id
    assert review.cycle_id == world.setup.cycle.pk
    assert review.reviewer_id == world.setup.graph.actor.pk
    with tenant_context(world.tenant_id):
        event = CycleEvent.objects.get(type="fiscal.document.reviewed")
        assert event.payload["review_id"] == str(review.pk)
        assert "Synthetic internal review note" not in str(event.payload)
        assert model("FiscalDocumentReview", app="cycles").objects.count() == 1


def test_a_summary_cannot_be_accepted_but_can_request_its_missing_xml(
    world: CaptureWorld,
) -> None:
    review_api()
    fiscal = world.ingest(summary=summary())
    with pytest.raises(ValidationError, match="XML"):
        record(world, fiscal, "aceito")
    with tenant_context(world.tenant_id):
        assert not model("FiscalDocumentReview", app="cycles").objects.exists()
    review = record(world, fiscal, "requer_xml")
    assert review.decision == "requer_xml"
    assert fiscal.summary_only is True


@pytest.mark.parametrize("stale", ["cycle", "digest", "revision"])
def test_stale_review_inputs_refuse_before_any_write(
    world: CaptureWorld, stale: str
) -> None:
    review_api()
    fiscal = world.ingest()
    changes: dict[str, object] = {}
    if stale == "cycle":
        changes["expected_version"] = 0
    elif stale == "digest":
        changes["input_digest"] = "0" * 64
    else:
        old_revision = fiscal.current_revision_id
        fiscal = world.ingest(version=2)
        changes["revision_id"] = old_revision
    with pytest.raises(VersionConflict):
        record(world, fiscal, "aceito", **changes)
    with tenant_context(world.tenant_id):
        assert not model("FiscalDocumentReview", app="cycles").objects.exists()
        assert not CycleEvent.objects.filter(type="fiscal.document.reviewed").exists()


def test_an_unassigned_reviewer_cannot_review_another_clients_document(
    world: CaptureWorld,
) -> None:
    api = review_api()
    fiscal = world.ingest()
    with tenant_context(world.tenant_id):
        with pytest.raises((Http404, PermissionDenied)):
            api.record_fiscal_document_review(
                world.setup.graph.staff,
                world.setup.cycle.pk,
                fiscal.pk,
                revision_id=fiscal.current_revision_id,
                decision="aceito",
                input_digest=api.input_digest(fiscal),
                expected_version=world.setup.cycle.version,
            )
        assert not model("FiscalDocumentReview", app="cycles").objects.exists()


def test_reviews_are_append_only_and_a_new_revision_needs_a_new_decision(
    world: CaptureWorld,
) -> None:
    api = review_api()
    fiscal = world.ingest()
    first = record(world, fiscal, "aceito")
    with tenant_context(world.tenant_id):
        reviews = model("FiscalDocumentReview", app="cycles").objects
        with transaction.atomic(), pytest.raises(DatabaseError):
            reviews.filter(pk=first.pk).update(decision="rejeitado")
        with transaction.atomic(), pytest.raises(DatabaseError):
            reviews.filter(pk=first.pk).delete()
    newer = world.ingest(version=4)
    assert api.input_digest(newer) != first.input_digest
    with tenant_context(world.tenant_id):
        assert not reviews.filter(revision_id=newer.current_revision_id).exists()
        first.refresh_from_db()
        assert first.decision == "aceito"
    next_review = record(world, newer, "requer_xml")
    assert next_review.pk != first.pk
    assert next_review.revision_id != first.revision_id


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_table_owner_cannot_rewrite_or_delete_a_fiscal_review(
    world: CaptureWorld, operation: str
) -> None:
    review = record(world, world.ingest(), "aceito")
    statement = (
        "UPDATE cycles_fiscaldocumentreview SET decision = 'rejeitado' WHERE id = %s"
        if operation == "update"
        else "DELETE FROM cycles_fiscaldocumentreview WHERE id = %s"
    )
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
        try:
            cursor.execute(
                "SELECT current_user, pg_get_userbyid(relowner) FROM pg_class "
                "WHERE oid = 'cycles_fiscaldocumentreview'::regclass"
            )
            login, owner = cursor.fetchone()
            assert login == owner
            assert login != "app_runtime"
            cursor.execute(
                "SELECT COUNT(*) FROM cycles_fiscaldocumentreview WHERE id = %s",
                [review.pk],
            )
            assert cursor.fetchone() == (1,)
            with (
                pytest.raises(DatabaseError, match="append-only") as refused,
                transaction.atomic(),
            ):
                cursor.execute(statement, [review.pk])
            assert getattr(refused.value.__cause__, "sqlstate", None) == "42501"
        finally:
            cursor.execute("SET ROLE app_runtime")


def test_queue_contains_captured_documents_with_origin_and_a_review_link(
    world: CaptureWorld,
) -> None:
    fiscal = world.ingest()
    response = world.setup.firm().get(reverse("document-queue") + "?fila=fiscais")
    assert response.status_code == 200
    text = response.content.decode()
    assert f'data-fiscal-document="{fiscal.pk}"' in text
    assert "Capturado do provedor" in text
    assert reverse("document-review", args=[fiscal.pk]) in text
    assert "Enviado pelo cliente" not in text


def test_review_workspace_get_and_post_use_the_existing_document_route(
    world: CaptureWorld,
) -> None:
    fiscal = world.ingest()
    browser = world.setup.firm()
    path = reverse("document-review", args=[fiscal.pk])
    response = browser.get(path)
    assert response.status_code == 200
    assert b"data-fiscal-review" in response.content
    api = review_api()
    with tenant_context(world.tenant_id):
        cycle = ServiceCycle.objects.get(pk=world.setup.cycle.pk)
        digest = api.input_digest(fiscal)
    response = browser.post(
        path,
        {
            "cycle_id": str(cycle.pk),
            "revision_id": str(fiscal.current_revision_id),
            "decision": "aceito",
            "expected_version": cycle.version,
            "input_digest": digest,
            "note": "Synthetic internal note",
        },
    )
    assert response.status_code == 302
    with tenant_context(world.tenant_id):
        review = model("FiscalDocumentReview", app="cycles").objects.get()
        assert review.decision == "aceito"


def test_review_preview_binds_one_snapshot_during_capture(world: CaptureWorld) -> None:
    fiscal = world.ingest()
    with tenant_context(world.tenant_id):
        first_digest = review_api().input_digest(fiscal)
    browser = world.setup.firm()
    browser.raise_request_exception = False
    path = reverse("document-review", args=[fiscal.pk])

    def capture() -> None:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET ROLE app_runtime")
            world.ingest(version=2)
        finally:
            connection.close()

    def advance_after_load(actor: User, client_id: UUID) -> ClientCompany:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(capture).result(timeout=30)
        return load_visible_client(actor, client_id)

    # The view has fetched R1 when it reaches authorization; R2 commits on
    # another connection before preview rendering can look up material again.
    with patch("apps.cycles.fiscal_views.load_visible_client", advance_after_load):
        response = browser.get(path)
    with tenant_context(world.tenant_id):
        assert (
            type(fiscal)
            .objects.filter(pk=fiscal.pk, current_revision__provider_version="2")
            .exists()
        )
    assert response.status_code == 200
    fields = HiddenInputs(response.content.decode()).values
    assert fields["revision_id"] == str(fiscal.current_revision_id)
    assert fields["input_digest"] == first_digest
    assert response.context["fiscal"].current_revision.provider_version == "1"

    stale = browser.post(path, {**fields, "decision": "aceito"})
    assert stale.status_code == 409
    refreshed = HiddenInputs(stale.content.decode()).values
    with tenant_context(world.tenant_id):
        fiscal.refresh_from_db()
        assert fiscal.current_revision is not None
        assert fiscal.current_revision.provider_version == "2"
        assert refreshed["revision_id"] == str(fiscal.current_revision_id)
        assert refreshed["input_digest"] == review_api().input_digest(fiscal)
        assert not model("FiscalDocumentReview", app="cycles").objects.exists()
        assert not CycleEvent.objects.filter(type="fiscal.document.reviewed").exists()


def test_cycle_renders_period_fiscal_documents_and_excludes_other_months(
    world: CaptureWorld,
) -> None:
    fiscal = world.ingest()
    response = world.setup.firm().get(
        reverse("cycle-detail", args=[world.setup.cycle.pk])
    )
    assert response.status_code == 200
    assert b"data-fiscal-documents" in response.content
    assert f'data-fiscal-document="{fiscal.pk}"'.encode() in response.content
    with tenant_context(world.tenant_id):
        type(fiscal).objects.filter(pk=fiscal.pk).update(issue_date="2026-08-31")
    response = world.setup.firm().get(
        reverse("cycle-detail", args=[world.setup.cycle.pk])
    )
    assert response.status_code == 200
    assert f'data-fiscal-document="{fiscal.pk}"'.encode() not in response.content


def test_briefing_surfaces_captured_documents_pending_review(
    world: CaptureWorld,
) -> None:
    fiscal = world.ingest()
    with tenant_context(world.tenant_id):
        context = SourceContext(
            clients=ClientCompany.objects.filter(pk=world.link.client_id), now=START
        )
        captured = [
            row for row in source_items(context) if row.kind == "fiscal_document"
        ]
    assert len(captured) == 1
    assert captured[0].period == MONTH
    assert captured[0].subject_ref == fiscal.pk
    assert "capturado" in captured[0].title.lower()
    path = reverse("document-review", args=[fiscal.pk])
    assert captured[0].url == f"{path}?ciclo={world.setup.cycle.pk}"


def test_captured_queue_and_cycle_section_are_inert_when_the_flag_is_off(
    world: CaptureWorld,
) -> None:
    fiscal = world.ingest()
    world.setup.graph.tenant.features = ["cycles_r1", "scan_required"]
    world.setup.graph.tenant.save(update_fields=["features"])
    queue = world.setup.firm().get(reverse("document-queue") + "?fila=fiscais")
    cycle = world.setup.firm().get(reverse("cycle-detail", args=[world.setup.cycle.pk]))
    assert queue.status_code == cycle.status_code == 200
    assert f'data-fiscal-document="{fiscal.pk}"'.encode() not in queue.content
    assert b"data-fiscal-documents" not in cycle.content
