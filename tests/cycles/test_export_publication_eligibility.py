"""Live original eligibility must survive from claim through publication."""

import io
import json
import zipfile
from functools import partial
from pathlib import Path
from uuid import UUID

import pytest
from django.core.files import File
from django.core.files.storage import default_storage
from django.db import OperationalError, connection
from django.db.models import Model
from django.urls import reverse
from psycopg.errors import LockNotAvailable
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.authz.models import Role
from apps.clients.models import ClientAssignment
from apps.core.tenancy import tenant_context
from apps.cycles import export_service
from apps.cycles.effects.executors import execute
from apps.cycles.evidence import ReviewDecision
from apps.cycles.export_records import ExportAttempt, ExportState
from apps.cycles.export_service import request_export
from apps.cycles.export_tasks import build_export
from apps.cycles.models import (
    CycleEvent,
    CycleState,
    CycleTask,
    DocumentIntake,
    EvidenceLink,
    ServiceCycle,
    TaskState,
)
from apps.cycles.services import cycle as cycle_service
from apps.cycles.services.approvals import approve
from apps.cycles.services.completion import propose_reopen
from apps.cycles.services.review import record_review
from apps.delivery.tasks import dispatch_tenant
from apps.obligations.models import Document
from apps.tenants.models import Membership
from tests.browser.journeys import smtp_at
from tests.cycles.completion_fixtures import CompletionSetup, close_cycle
from tests.cycles.document_fixtures import fixture, reviewed
from tests.delivery.smtp import FakeSMTP
from tests.portal.request_fixtures import PortalSetup, portal_setup
from tests.support_threads import run_in_thread_as_runtime


def _lock_row_nowait(model: type[Model], identity: object) -> Model:
    return model._default_manager.select_for_update(nowait=True).get(pk=identity)


pytestmark = pytest.mark.django_db(transaction=True)


def completed_supersession(
    settings: SettingsWrapper, tmp_path: Path
) -> tuple[PortalSetup, DocumentIntake, UUID]:
    """Preserve a clean unreviewed original beside its accepted replacement."""
    settings.MEDIA_ROOT = tmp_path
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    settings.DELIVERY_PAUSED = False
    setup = portal_setup("export-publication-race")
    original, link = reviewed(setup, "nfe_emitente_cliente.xml")
    _, replacement = reviewed(
        setup, "replacement.xml", fixture("nfe_emitente_cliente.xml") + b"\n"
    )
    with FakeSMTP(mode="accept") as peer, smtp_at(peer.port):
        dispatch_tenant(str(setup.graph.tenant.pk))
    with tenant_context(setup.graph.tenant.pk):
        setup.request.required = False
        setup.request.save(update_fields=["required"])
        record_review(
            setup.graph.actor,
            replacement.pk,
            decision=ReviewDecision.ACCEPTED,
            client_reason="",
            corrections=[],
            expected_version=setup.request.version,
        )
        ClientAssignment.objects.create(
            tenant=setup.graph.tenant, client=setup.graph.client, user=setup.graph.staff
        )
        CycleTask.objects.filter(cycle=setup.cycle).update(state=TaskState.ACCEPTED)
        ServiceCycle.objects.filter(pk=setup.cycle.pk).update(
            state=CycleState.EXECUTION
        )
        close_cycle(CompletionSetup(setup.graph, setup.cycle))
        setup.cycle.refresh_from_db()
        assert setup.cycle.state == CycleState.COMPLETED
    return setup, original, link.pk


def reopen_and_reject(setup: PortalSetup, link_id: UUID) -> None:
    """Commit a real independently approved amendment, then a real rejection."""
    assert not connection.in_atomic_block
    with tenant_context(setup.graph.tenant.pk):
        reviewer = User.objects.create_user(email="reviewer@export-publication.test")
        Membership.objects.create(
            tenant=setup.graph.tenant, user=reviewer, role=Role.OPERATIONS_ADMIN
        )
        setup.cycle.refresh_from_db()
        proposal = propose_reopen(
            setup.graph.actor,
            setup.cycle.pk,
            reason="Conferir o original substituído.",
            affected_scope=(setup.cycle.pk,),
            expected_version=setup.cycle.version,
        )
        decision = approve(reviewer, proposal.pk, expected_version=proposal.version)
        assert execute(
            reviewer, proposal.pk, expected_version=decision.proposal.version
        ).executed
        setup.request.refresh_from_db()
        record_review(
            setup.graph.actor,
            link_id,
            decision=ReviewDecision.REJECTED,
            client_reason="Este original foi substituído.",
            corrections=[],
            expected_version=setup.request.version,
        )


@pytest.mark.parametrize("boundary", ["open", "save"])
def test_historical_rejection_during_build_refuses_publication(
    settings: SettingsWrapper,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    # Given a concluded cycle whose preserved generation contains two clean files.
    setup, original, link_id = completed_supersession(settings, tmp_path)
    with tenant_context(setup.graph.tenant.pk):
        export = request_export(
            setup.graph.actor, setup.cycle.pk, expected_version=setup.cycle.version
        )
        index_id = export.index_id
    open_file, save_file = default_storage.open, default_storage.save
    rejected: list[UUID] = []

    def reject_once() -> None:
        if not rejected:
            reopen_and_reject(setup, link_id)
            rejected.append(link_id)

    def open_after_rejection(name: str, mode: str = "rb") -> File[bytes]:
        reject_once()
        return open_file(name, mode)

    def reject_after_upload(
        name: str, content: File[bytes], max_length: int | None = None
    ) -> str:
        saved = save_file(name, content, max_length=max_length)
        reject_once()
        return saved

    monkeypatch.setattr(
        default_storage,
        boundary,
        open_after_rejection if boundary == "open" else reject_after_upload,
    )

    # When real review commits after claim, before source I/O or after ZIP upload.
    build_export(str(setup.graph.tenant.pk), str(export.pk))

    # Then no archive is published or downloadable and its owned upload is cleaned.
    assert rejected == [link_id]
    with tenant_context(setup.graph.tenant.pk):
        export.refresh_from_db()
        assert export.state == ExportState.FAILED, (
            "An original rejected during a historical build reached publication."
        )
        assert export.error_code == "original_ineligible"
        assert export.storage_key == ""
        attempts = list(ExportAttempt.objects.filter(export=export))
        assert attempts
        assert all(not row.cleanup_pending for row in attempts)
    assert all(not default_storage.exists(row.storage_key) for row in attempts)
    page = setup.firm().get(reverse("evidence-export-detail", args=[export.pk]))
    assert page.status_code == 200
    assert page.context["ready"] is False
    download = setup.firm().get(reverse("evidence-export-download", args=[export.pk]))
    assert download.status_code == 409

    # A fresh claim retries this export, preserving the index and naming the exclusion.
    monkeypatch.undo()
    build_export(str(setup.graph.tenant.pk), str(export.pk))
    with tenant_context(setup.graph.tenant.pk):
        export.refresh_from_db()
        assert export.state == ExportState.READY, export.error_code
        assert export.index_id == index_id
        assert export.excluded_originals[0]["id"] == str(original.document_id)
        assert export.excluded_originals[0]["code"] == ReviewDecision.REJECTED
    download = setup.firm().get(reverse("evidence-export-download", args=[export.pk]))
    assert download.status_code == 200
    with zipfile.ZipFile(io.BytesIO(download.getvalue())) as archive:
        assert f"originals/{original.document_id}.xml" not in archive.namelist()
        assert (
            len(
                [
                    member
                    for member in archive.namelist()
                    if member.startswith("originals/")
                ]
            )
            == 1
        )
        assert (
            json.loads(archive.read("excluded-originals.json"))
            == export.excluded_originals
        )


def test_publication_holds_every_included_original_eligibility_lock(
    settings: SettingsWrapper,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a historical archive with two eligible originals.
    setup, _, _ = completed_supersession(settings, tmp_path)
    with tenant_context(setup.graph.tenant.pk):
        export = request_export(
            setup.graph.actor, setup.cycle.pk, expected_version=setup.cycle.version
        )
        document_ids = list(
            EvidenceLink.objects.filter(request=setup.request).values_list(
                "document_id", flat=True
            )
        )
        intake_ids = list(
            DocumentIntake.objects.filter(document_id__in=document_ids).values_list(
                "pk", flat=True
            )
        )
        link_ids = list(
            EvidenceLink.objects.filter(request=setup.request).values_list(
                "pk", flat=True
            )
        )
    emit = cycle_service.emit
    observed: list[str] = []

    def observe_publication(
        cycle: ServiceCycle,
        actor: User,
        event_type: str,
        *,
        payload: dict[str, str | int],
    ) -> CycleEvent:
        if event_type == "evidence.export_ready":
            assert connection.in_atomic_block
            for model, identities in (
                (Document, document_ids),
                (DocumentIntake, intake_ids),
                (EvidenceLink, link_ids),
            ):
                assert len(identities) == 2
                for identity in identities:
                    with pytest.raises(OperationalError) as failure:
                        run_in_thread_as_runtime(
                            setup.graph.tenant.pk,
                            partial(_lock_row_nowait, model, identity),
                        )
                    assert isinstance(failure.value.__cause__, LockNotAvailable)
            observed.append(event_type)
        return emit(cycle, actor, event_type, payload=payload)

    monkeypatch.setattr(export_service, "emit", observe_publication)

    # When the real finalizer publishes, the observer tests all six locks in PG.
    build_export(str(setup.graph.tenant.pk), str(export.pk))

    # Then no included document, intake or link was released before publication.
    assert observed == ["evidence.export_ready"]
    with tenant_context(setup.graph.tenant.pk):
        export.refresh_from_db()
        assert export.state == ExportState.READY, export.error_code
