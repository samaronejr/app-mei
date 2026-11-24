"""Prove the private writer and material before the readers activate corrections."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from threading import Event
from typing import TYPE_CHECKING, cast

import pytest
from django.core.exceptions import ValidationError
from django.db import connection, connections
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.authz.models import Role
from apps.clients.models import ClientAssignment
from apps.core.tenancy import tenant_context
from apps.cycles.effects.executors import execute
from apps.cycles.errors import IllegalTransition, VersionConflict
from apps.cycles.fiscal_records import FiscalDocumentReview
from apps.cycles.fiscal_scope import review_cycles
from apps.cycles.models import (
    CycleState,
    CycleTask,
    EvidenceRequest,
    RequestState,
    TaskState,
)
from apps.cycles.records import CycleEvent, ServiceCycle
from apps.cycles.services.approvals import approve
from apps.cycles.services.completion import propose_reopen
from apps.delivery.tasks import dispatch_tenant
from apps.obligations.models import RevisionArtifact
from apps.tenants.models import Membership
from tests.browser.journeys import smtp_at
from tests.cycles.approval_fixtures import SIGNAL_TIMEOUT
from tests.cycles.completion_fixtures import CompletionSetup, close_cycle
from tests.cycles.fiscal_review_fixtures import (
    blank_review_notes,
    insert_review_edge,
    row_image,
)
from tests.cycles.fiscal_review_race_fixtures import review_race
from tests.cycles.test_fiscal_document_review import record, world
from tests.cycles.test_fiscal_review_storage import signal_statement
from tests.cycles.test_playbook_http_conflicts import HiddenInputs
from tests.delivery.smtp import FakeSMTP
from tests.fakes.focus import FakeFocus
from tests.integrations.capture_fixtures import (
    MONTH,
    START,
    capture_api,
    page,
    review_api,
)
from tests.isolation.axorim_seed import make_seed_context, seed_model
from tests.obligations.fiscal_fixtures import nfse
from tests.obligations.test_fiscal_documents import ingest

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from pytest_django.fixtures import SettingsWrapper

    from apps.obligations.models import FiscalDocument
    from tests.integrations.capture_fixtures import CaptureWorld

__all__ = ["world"]
pytestmark = pytest.mark.django_db(transaction=True)


def material(fiscal: FiscalDocument) -> str:
    return cast("str", review_api().input_digest(fiscal))


def _prepared(
    world: CaptureWorld,
    fiscal: FiscalDocument,
    head: FiscalDocumentReview | None = None,
) -> Callable[..., FiscalDocumentReview]:
    with tenant_context(world.tenant_id):
        cycle = ServiceCycle.objects.get(pk=world.setup.cycle.pk)
        params = {
            "revision_id": fiscal.current_revision_id,
            "decision": "rejeitado",
            "input_digest": material(fiscal),
            "expected_version": cycle.version,
            "expected_head_id": head.pk if head else None,
            "note": "Synthetic reason",
        }

    def submit(**overrides: object) -> FiscalDocumentReview:
        api = review_api()
        append = getattr(api, "_append_review", api.record_fiscal_document_review)
        with tenant_context(world.tenant_id):
            return cast(
                "FiscalDocumentReview",
                append(
                    world.setup.graph.actor,
                    cycle.pk,
                    fiscal.pk,
                    **(params | overrides),
                ),
            )

    return submit


def legacy_digest(revision_id: UUID) -> str:
    original = RevisionArtifact.objects.select_related("document", "revision").get(
        revision_id=revision_id, role="xml_original"
    )
    return hashlib.sha256(
        f"{original.revision.provider_version}:{original.document.sha256}".encode()
    ).hexdigest()


def test_private_writer_binds_material_and_preserves_the_original(
    world: CaptureWorld,
) -> None:
    fiscal = world.ingest()
    revision = fiscal.current_revision_id
    assert revision is not None
    # The legacy public digest exists on both historical heads; the new material
    # protocol must distinguish itself before the first successor is writable.
    with tenant_context(world.tenant_id):
        legacy = legacy_digest(revision)
        api = review_api()
        digest = api.input_digest(fiscal)
        assert digest != legacy
    root = _prepared(world, fiscal)(note="")
    with tenant_context(world.tenant_id):
        before = row_image(root.pk)
        _, selected, eligible = review_cycles(fiscal, root.cycle_id)
        assert selected is not None
        assert selected.pk == root.cycle_id
        assert eligible
    correct = _prepared(world, fiscal, root)
    with pytest.raises(VersionConflict):
        correct(input_digest=legacy)
    successor = correct(decision="aceito", note="\u00a0 Acentuação. \u202f")
    assert successor.supersedes_id == root.pk
    assert successor.note == "Acentuação."
    assert successor.input_digest == root.input_digest == digest
    with tenant_context(world.tenant_id):
        assert row_image(root.pk) == before
        assert CycleEvent.objects.filter(type="fiscal.document.reviewed").count() == 2
    with pytest.raises(VersionConflict):
        _prepared(
            world, fiscal, root
        )()  # A fresh cycle version cannot bypass head CAS.
    third = _prepared(world, fiscal, successor)(decision="requer_xml")
    assert third.supersedes_id == successor.pk


def test_a_legacy_form_without_a_head_cannot_correct_an_existing_review(
    world: CaptureWorld,
) -> None:
    fiscal = world.ingest()
    cycle = world.setup.cycle
    browser = world.setup.firm()
    path = reverse("document-review", args=[fiscal.pk])
    visible = browser.get(path)
    fields = HiddenInputs(visible.content.decode()).values
    fields.update(decision="rejeitado", note="Synthetic reason")
    fields.pop("expected_head_id", None)
    assert browser.post(path, fields).status_code == 302
    with tenant_context(world.tenant_id):
        root = FiscalDocumentReview.objects.get()
        cycle.refresh_from_db()
        fields["expected_version"] = str(cycle.version)
    assert browser.post(path, fields).status_code == 409
    with tenant_context(world.tenant_id):
        assert list(FiscalDocumentReview.objects.values_list("pk", flat=True)) == [
            root.pk
        ]


def test_service_invoice_review_uses_competence_not_issue_month(
    world: CaptureWorld,
) -> None:
    fiscal = cast(
        "FiscalDocument",
        ingest(
            world.setup,
            source="importacao_equipe",
            client_role="tomador",
            payload=nfse().replace(b"2026-09-15", b"2026-10-15"),
        ),
    )
    assert fiscal.issue_date == date(2026, 10, 15)
    assert fiscal.competence == MONTH
    assert record(world, fiscal, "aceito").cycle_id == world.setup.cycle.pk


@pytest.mark.parametrize("change", ["blank", "closed", "material"])
def test_private_writer_refusals_do_not_append(
    world: CaptureWorld, change: str
) -> None:
    fiscal = world.ingest()
    root = _prepared(world, fiscal)()
    submit = _prepared(world, fiscal, root)
    if change == "blank":
        for note in blank_review_notes():
            with pytest.raises(ValidationError):
                submit(note=note)
    elif change == "closed":
        with tenant_context(world.tenant_id):
            ServiceCycle.objects.filter(pk=root.cycle_id).update(
                state=CycleState.COMPLETED
            )
        with pytest.raises(IllegalTransition):
            submit()
    else:
        with tenant_context(world.tenant_id):
            type(fiscal).objects.filter(pk=fiscal.pk).update(fiscal_state="cancelada")
        with pytest.raises(VersionConflict):
            submit()
    with tenant_context(world.tenant_id):
        assert list(FiscalDocumentReview.objects.values_list("pk", flat=True)) == [
            root.pk
        ]
        assert CycleEvent.objects.filter(type="fiscal.document.reviewed").count() == 1


def test_concurrent_private_corrections_have_a_single_winner(
    world: CaptureWorld,
) -> None:
    fiscal = world.ingest()
    root = _prepared(world, fiscal)()
    submit = _prepared(world, fiscal, root)
    winner, loser = review_race(submit, submit)
    successor = winner.result(SIGNAL_TIMEOUT)
    with pytest.raises(VersionConflict):
        loser.result(SIGNAL_TIMEOUT)
    assert successor.supersedes_id == root.pk
    with tenant_context(world.tenant_id):
        assert FiscalDocumentReview.objects.count() == 2
        assert CycleEvent.objects.filter(type="fiscal.document.reviewed").count() == 2


def test_existing_approved_reopen_permits_private_correction(
    world: CaptureWorld,
    settings: SettingsWrapper,
) -> None:
    fiscal = world.ingest()
    root = _prepared(world, fiscal)()
    cycle, graph = world.setup.cycle, world.setup.graph
    settings.DELIVERY_PAUSED = False
    with FakeSMTP(mode="accept") as peer, smtp_at(peer.port):
        dispatch_tenant(str(world.tenant_id))
    with tenant_context(world.tenant_id):
        ClientAssignment.objects.get_or_create(
            tenant_id=cycle.tenant_id,
            client_id=cycle.client_id,
            user=graph.staff,
        )
        CycleTask.objects.filter(cycle=cycle).update(state=TaskState.ACCEPTED)
        EvidenceRequest.objects.filter(cycle=cycle).update(state=RequestState.ACCEPTED)
        ServiceCycle.objects.filter(pk=cycle.pk).update(state=CycleState.EXECUTION)
        close_cycle(CompletionSetup(graph, cycle))
        cycle.refresh_from_db()
    with pytest.raises(IllegalTransition):
        _prepared(world, fiscal, root)()
    with tenant_context(world.tenant_id):
        assert FiscalDocumentReview.objects.count() == 1
        reviewer = User.objects.create_user(email="review-reopen@example.test")
        Membership.objects.create(
            tenant=graph.tenant, user=reviewer, role=Role.OPERATIONS_ADMIN
        )
        proposal = propose_reopen(
            graph.actor,
            cycle.pk,
            reason="Retomar revisão fiscal.",
            affected_scope=(cycle.pk,),
            expected_version=cycle.version,
        )
        approval = approve(reviewer, proposal.pk, expected_version=proposal.version)
        assert execute(
            reviewer, proposal.pk, expected_version=approval.proposal.version
        ).executed
    assert _prepared(world, fiscal, root)().supersedes_id == root.pk


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("issue_date", MONTH),
        ("counterpart_id", "12345678000195"),
        ("role_verification", "unconfirmed"),
        ("total", "17.32"),
        ("iss_retention_party", "intermediario"),
        ("fiscal_state", "cancelada"),
    ],
)
def test_digest_changes_with_material_facts(
    world: CaptureWorld,
    field: str,
    value: object,
) -> None:
    fiscal = world.ingest()
    before = material(fiscal)
    with tenant_context(world.tenant_id):
        type(fiscal).objects.filter(pk=fiscal.pk).update(**{field: value})
        fiscal.refresh_from_db()
    assert material(fiscal) != before
    assert material(world.ingest(version=2)) != before


def test_digest_changes_when_an_existing_role_gains_a_different_hash(
    world: CaptureWorld,
) -> None:
    fiscal = world.ingest()
    with tenant_context(world.tenant_id):
        original = RevisionArtifact.objects.select_related("document").get(
            revision_id=fiscal.current_revision_id,
        )
        fields = {
            "tenant_id": fiscal.tenant_id,
            "client_id": fiscal.client_id,
            "revision_id": original.revision_id,
            "role": "xml_cce",
        }
        RevisionArtifact.objects.create(document=original.document, **fields)
        before = material(fiscal)
        extra = type(original.document).objects.create(
            tenant_id=fiscal.tenant_id,
            client_id=fiscal.client_id,
            sha256=hashlib.sha256(b"distinct retained CC-e bytes").hexdigest(),
            original_filename="second-cce.xml",
            portal_visible=False,
            content_type="application/xml",
            byte_size=30,
            acquisition_origin="upload_equipe",
            uploaded_by_id=world.setup.graph.actor.pk,
            client_upload=False,
        )
        assert extra.sha256 != original.document.sha256
        RevisionArtifact.objects.create(document=extra, **fields)
    assert material(fiscal) != before


def test_poll_and_bookkeeping_leave_a_current_review_current(
    world: CaptureWorld,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fiscal = world.ingest()
    root = _prepared(world, fiscal)()
    stream = world.stream()
    with FakeFocus((page([]), page([]))) as fake:
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        capture_api().synchronize(world.tenant_id, stream.pk)
        assert world.reload(stream).last_api_success_at == START
        later = START + timedelta(hours=1)
        monkeypatch.setattr(timezone, "now", lambda: later)
        capture_api().synchronize(world.tenant_id, stream.pk)
        assert len(fake.requests) == 2
    assert world.reload(stream).last_api_success_at == later
    with tenant_context(world.tenant_id):
        type(fiscal).objects.filter(pk=fiscal.pk).update(
            version=fiscal.version + 1,
            created_at=fiscal.created_at + timedelta(days=1),
        )
        fiscal.refresh_from_db()
    assert material(fiscal) == root.input_digest


def test_populated_review_migration_roundtrip_and_successor_refusal(
    world: CaptureWorld,
) -> None:
    fiscal = world.ingest()
    revision = fiscal.current_revision_id
    assert revision is not None
    root = _prepared(world, fiscal)()
    executor = MigrationExecutor(connection)
    parent = executor.loader.project_state([("cycles", "0023_fiscal_document_review")])
    migration = executor.loader.get_migration("cycles", "0024_fiscal_review_successor")
    stored = parent.apps.get_model("cycles", "FiscalDocumentReview")._base_manager
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
        try:
            before = stored.values().get(pk=root.pk)
            executor.unapply_migration(parent, migration)
            try:
                assert stored.values().get(pk=root.pk) == before
            finally:
                executor.apply_migration(parent.clone(), migration)
            assert stored.values().get(pk=root.pk) == before
            assert (
                FiscalDocumentReview.all_objects.get(pk=root.pk).supersedes_id is None
            )
        finally:
            cursor.execute("SET ROLE app_runtime")
    successor = _prepared(world, fiscal, root)()
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
        try:
            with pytest.raises(IrreversibleError):
                executor.unapply_migration(parent, migration)
        finally:
            cursor.execute("SET ROLE app_runtime")
    with tenant_context(world.tenant_id):
        assert (
            FiscalDocumentReview.objects.get(pk=successor.pk).supersedes_id == root.pk
        )


def test_migration_reverse_waits_for_a_successor_in_another_tenant(
    world: CaptureWorld,
) -> None:
    ctx = make_seed_context("foreign-review-reverse", "foreign-a")
    reached = Event()
    with tenant_context(ctx.tenant.pk):
        root = cast("FiscalDocumentReview", seed_model(FiscalDocumentReview, ctx))

    def reverse_migration() -> None:
        try:
            executor = MigrationExecutor(connection)
            parent = executor.loader.project_state(
                [("cycles", "0023_fiscal_document_review")]
            )
            migration = executor.loader.get_migration(
                "cycles", "0024_fiscal_review_successor"
            )
            with (
                tenant_context(world.tenant_id),
                connection.execute_wrapper(
                    signal_statement("IN ACCESS EXCLUSIVE MODE", reached)
                ),
            ):
                executor.unapply_migration(parent, migration)
            executor.apply_migration(parent.clone(), migration)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with tenant_context(ctx.tenant.pk):
            key = insert_review_edge(root, root.pk)
            rollback = executor.submit(reverse_migration)
            assert reached.wait(SIGNAL_TIMEOUT)
        with pytest.raises(IrreversibleError):
            rollback.result(SIGNAL_TIMEOUT)
    with tenant_context(ctx.tenant.pk):
        assert FiscalDocumentReview.objects.get(pk=key).supersedes_id == root.pk
