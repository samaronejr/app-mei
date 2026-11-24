"""Firm-only request identity, attribution, and storage invariants (AXRM-049)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from typing import TYPE_CHECKING

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, connections, transaction
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.http import Http404
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.cycles import receipt_observations
from apps.cycles.errors import IllegalTransition
from apps.cycles.evidence import (
    EvidenceLink,
    EvidenceRequest,
    RequestInternal,
    RequestKind,
    RequestState,
)
from apps.cycles.extraction.service import ExtractionOutcome, extract_intake
from apps.cycles.models import CycleEvent, CycleState, ServiceCycle
from apps.cycles.models import Exception as CycleException
from apps.cycles.scanning import FakeScanner, ScanOutcome, scan_intake
from apps.cycles.services import requests
from apps.cycles.services.proposed_transitions import propose_waiver
from apps.cycles.services.review import record_review
from apps.obligations.models import SourceObservation
from tests.cycles.approval_fixtures import SIGNAL_TIMEOUT, LockSignal
from tests.cycles.internal_request_fixtures import (
    attach_filing,
    filing_subject,
    prior_ordinary_cutover,
    upload_for_internal_review,
)
from tests.cycles.portal_upgrade_fixtures import migration_owner
from tests.portal.request_fixtures import PortalSetup, portal_setup

if TYPE_CHECKING:
    from pathlib import Path
    from uuid import UUID

    from pytest_django.fixtures import SettingsWrapper

pytestmark = pytest.mark.django_db(transaction=True)
GUIDE = "guia_das_oficial"
PERIOD = "documentos_fiscais_do_periodo"
MUNICIPAL = "comprovante_municipal"
OLD = ("cycles", "0027_portal_request_history")
NEW = ("cycles", "0028_internal_request_purposes")
PURPOSE_CHECK = "evidencerequest_internal_purpose_check"


@pytest.fixture
def setup() -> PortalSetup:
    return portal_setup("internal-requests")


def test_system_request_reuses_one_identity_and_records_no_human(
    setup: PortalSetup,
) -> None:
    with tenant_context(setup.graph.tenant.pk):
        version = setup.cycle.version
        ensure = requests.ensure_internal_request
        request = ensure(setup.cycle, PERIOD)
        assert ensure(setup.cycle, PERIOD, actor=setup.graph.actor).pk == request.pk
        assert request.purpose == PERIOD
        assert request.state == RequestState.INTERNAL
        assert not request.required
        assert request.created_by_id is None
        assert not request.client_note
        assert request.requested_at is None
        companion = RequestInternal.objects.get(request=request)
        assert companion.instructions.strip()
        [event] = CycleEvent.objects.filter(
            aggregate_id=request.pk, type="request.created"
        )
        assert event.actor_id is None
        assert event.payload == {"purpose": PERIOD}
        setup.cycle.refresh_from_db()
        assert setup.cycle.version == version + 1


def test_closed_cycle_can_supply_its_late_change_request(setup: PortalSetup) -> None:
    with tenant_context(setup.graph.tenant.pk):
        ServiceCycle.objects.filter(pk=setup.cycle.pk).update(
            state=CycleState.COMPLETED
        )
        request = requests.ensure_internal_request(setup.cycle, PERIOD)
        assert setup.cycle.requests.get(pk=request.pk).purpose == PERIOD
        assert request.cycle.state == CycleState.COMPLETED


@pytest.mark.parametrize("scanner_down", [True, False], ids=["scanner", "extraction"])
def test_system_request_intake_assigns_problem_to_actual_staff_submitter(
    setup: PortalSetup, settings: SettingsWrapper, tmp_path: Path, scanner_down: bool
) -> None:
    settings.MEDIA_ROOT = tmp_path
    graph = setup.graph
    with tenant_context(graph.tenant.pk):
        request = requests.ensure_internal_request(setup.cycle, PERIOD)
        ServiceCycle.objects.filter(pk=setup.cycle.pk).update(owner=None)
        intake = upload_for_internal_review(setup, request)
    now = intake.created_at + timedelta(days=2)
    outcome = scan_intake(
        graph.tenant.pk, intake.pk, scanner=FakeScanner(down=scanner_down), now=now
    )
    if scanner_down:
        assert outcome == ScanOutcome.UNAVAILABLE
    else:
        assert outcome == ScanOutcome.VERIFIED
        assert extract_intake(graph.tenant.pk, intake.pk, now=timezone.now()) == (
            ExtractionOutcome.DIVERGENT
        )
    with tenant_context(graph.tenant.pk):
        problem = CycleException.objects.get(cycle=setup.cycle, subject_ref=intake.pk)
        assert problem.owner_id == graph.actor.pk
        assert not problem.client_visible
        assert EvidenceRequest.objects.get(pk=request.pk).created_by_id is None


def test_staff_attribution_and_internal_lifecycle(setup: PortalSetup) -> None:
    with tenant_context(setup.graph.tenant.pk):
        actor = setup.graph.actor
        request = requests.ensure_internal_request(setup.cycle, GUIDE, actor=actor)
        assert request.created_by_id == actor.pk
        event = CycleEvent.objects.get(aggregate_id=request.pk)
        assert event.actor_id == actor.pk
        assert RequestInternal.objects.get(request=request).instructions.strip()
        for refuse in (requests.waive_request, propose_waiver):
            with pytest.raises(IllegalTransition):
                refuse(actor, request.pk, reason="Motivo", expected_version=1)
        request.refresh_from_db()
        assert request.state == RequestState.INTERNAL


def test_concurrent_callers_take_the_cycle_lock_and_return_one_row(
    setup: PortalSetup,
) -> None:
    held, attempted, release = Event(), Event(), Event()
    first_sql = LockSignal("cycles_servicecycle", held, release=release, after=True)
    second_sql = LockSignal("cycles_servicecycle", attempted)

    def call(seam: LockSignal) -> UUID:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET ROLE app_runtime")
            with (
                tenant_context(setup.graph.tenant.pk),
                connection.execute_wrapper(seam),
            ):
                return requests.ensure_internal_request(setup.cycle, PERIOD).pk
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(call, first_sql)
        try:
            assert held.wait(SIGNAL_TIMEOUT), "First caller never locked the cycle."
            second = pool.submit(call, second_sql)
            assert attempted.wait(SIGNAL_TIMEOUT), "Second caller never tried the lock."
        finally:
            release.set()
        assert first.result(SIGNAL_TIMEOUT) == second.result(SIGNAL_TIMEOUT)
    with tenant_context(setup.graph.tenant.pk):
        [row] = EvidenceRequest.objects.filter(cycle=setup.cycle, purpose=PERIOD)
        assert RequestInternal.objects.filter(request=row).count() == 1
        assert CycleEvent.objects.filter(aggregate_id=row.pk).count() == 1


def test_only_municipal_purpose_can_bind_a_private_obligation(
    setup: PortalSetup,
) -> None:
    with tenant_context(setup.graph.tenant.pk):
        ensure = requests.ensure_internal_request
        subject = filing_subject(setup)
        with pytest.raises(ValidationError):
            requests.create_request(
                setup.graph.actor,
                setup.cycle.pk,
                requests.RequestFields(
                    "Recibo", kind=RequestKind.COMPROVANTE, obligation_id=subject.pk
                ),
                expected_version=setup.cycle.version,
            )
        assert not EvidenceRequest.objects.filter(obligation=subject).exists()
        for purpose in (GUIDE, PERIOD):
            with pytest.raises(ValidationError):
                ensure(setup.cycle, purpose, obligation=subject)
        with pytest.raises(ValidationError):
            ensure(setup.cycle, MUNICIPAL)
        with pytest.raises(ValidationError):
            ensure(setup.cycle, "undefined")
        request = ensure(setup.cycle, MUNICIPAL, obligation=subject)
        assert request.kind == RequestKind.COMPROVANTE
        assert request.obligation_id == subject.pk
        assert request.purpose == MUNICIPAL
        assert RequestInternal.objects.get(request=request).instructions.strip()
        assert ensure(setup.cycle, MUNICIPAL, obligation=subject).pk == request.pk


@pytest.mark.parametrize("case", ["other_client", "other_period", "public"])
def test_municipal_binding_rechecks_scope_and_visibility(
    setup: PortalSetup, case: str
) -> None:
    with tenant_context(setup.graph.tenant.pk):
        ensure = requests.ensure_internal_request
        overrides: dict[str, dict[str, object]] = {
            "other_client": {"client": setup.other_client},
            "other_period": {
                "competence_month": setup.cycle.period_start.replace(year=2024)
            },
            "public": {"portal_visible": True},
        }
        subject = filing_subject(setup, **overrides[case])
        expected = ValidationError if case == "public" else Http404
        with pytest.raises(expected):
            ensure(setup.cycle, MUNICIPAL, obligation=subject)
        assert not EvidenceRequest.objects.filter(purpose=MUNICIPAL).exists()


def test_municipal_filing_link_is_not_a_payment_submission(setup: PortalSetup) -> None:
    graph = setup.graph
    with tenant_context(graph.tenant.pk):
        request = requests.ensure_internal_request(
            setup.cycle, MUNICIPAL, obligation=filing_subject(setup)
        )
        link, intake = attach_filing(setup, request)
        assert receipt_observations.settle_pending(graph.tenant) == 0
        assert receipt_observations.settle_link(link.pk) is None
        observations = SourceObservation.objects.filter(evidence_link_id=link.pk)
        assert not observations.exists()
        record_review(
            graph.actor,
            link.pk,
            decision="aceito",
            client_reason="",
            corrections=[],
            expected_version=request.version,
            payment_relevant=True,
        )
        assert not observations.exists()
        assert not receipt_observations.payment_evidence_eligible(request, link, intake)


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"purpose": PERIOD}, "evidencerequest_purpose_uniq"),
        ({"purpose": "unknown"}, PURPOSE_CHECK),
        ({"purpose": PERIOD, "required": True}, PURPOSE_CHECK),
        ({"purpose": PERIOD, "state": "solicitado"}, PURPOSE_CHECK),
        ({"purpose": None}, "evidencerequest_human_or_purpose_check"),
    ],
)
def test_database_enforces_internal_identity_and_attribution(
    setup: PortalSetup, overrides: dict[str, str | bool | None], constraint: str
) -> None:
    with tenant_context(setup.graph.tenant.pk):
        request = requests.ensure_internal_request(setup.cycle, PERIOD)
        fields = {
            "tenant_id": request.tenant_id,
            "client_id": request.client_id,
            "cycle_id": request.cycle_id,
            "state": RequestState.INTERNAL,
            "title": "Different title",
            "required": False,
            **overrides,
        }
        with pytest.raises(IntegrityError, match=constraint), transaction.atomic():
            EvidenceRequest.objects.create(**fields)


def test_migration_adopts_only_the_legacy_guide(setup: PortalSetup) -> None:
    executor = MigrationExecutor(connection)
    assert NEW in executor.loader.graph.nodes, "The purpose migration is absent."
    old = executor.loader.project_state([OLD])
    migration = executor.loader.get_migration(*NEW)
    legacy = old.apps.get_model("cycles", "EvidenceRequest")._base_manager
    graph = setup.graph
    with migration_owner():
        executor.unapply_migration(old, migration)
        try:
            fields = {
                "tenant_id": graph.tenant.pk,
                "client_id": graph.client.pk,
                "cycle_id": setup.cycle.pk,
                "title": "Guia DAS oficial",
                "created_by_id": graph.actor.pk,
                "required": False,
            }
            namesake = legacy.create(**fields, state="interno")
            prior_ordinary_cutover(setup, namesake.pk)
            guide = legacy.create(**fields, state="interno")
            other = legacy.create(**fields, state="solicitado")
            duplicate = legacy.create(**fields, state="interno")
            peer_scope = {
                **fields,
                "client_id": setup.other_client.pk,
                "cycle_id": setup.other_request.cycle_id,
            }
            peer_namesake = legacy.create(**peer_scope, state="interno")
            recovered = legacy.create(
                **{**peer_scope, "title": "Renamed"}, state="interno"
            )
            old.apps.get_model("cycles", "CycleEvent")._base_manager.create(
                tenant_id=graph.tenant.pk,
                client_id=setup.other_client.pk,
                cycle_id=setup.other_request.cycle_id,
                actor_id=graph.actor.pk,
                type="request.created",
                aggregate_type="EvidenceRequest",
                aggregate_id=recovered.pk,
                aggregate_version=1,
                payload={"origin": "pgmei"},
            )
        finally:
            executor.apply_migration(old.clone(), migration)
    with tenant_context(graph.tenant.pk):
        expected = {row.pk: None for row in (namesake, peer_namesake, other, duplicate)}
        expected_purposes = {**expected, guide.pk: GUIDE, recovered.pk: GUIDE}
        retained = EvidenceRequest.objects.filter(pk__in=expected_purposes)
        assert dict(retained.values_list("pk", "purpose")) == expected_purposes
        assert requests.ensure_internal_request(setup.cycle, GUIDE).pk == guide.pk


@pytest.mark.parametrize(
    "staff_municipal", [False, True], ids=["system", "staff-municipal"]
)
def test_reverse_refuses_purposed_requests(
    setup: PortalSetup, staff_municipal: bool
) -> None:
    executor = MigrationExecutor(connection)
    assert NEW in executor.loader.graph.nodes
    old = executor.loader.project_state([OLD])
    migration = executor.loader.get_migration(*NEW)
    graph = setup.graph
    link = None
    with tenant_context(graph.tenant.pk):
        if staff_municipal:
            request = requests.ensure_internal_request(
                setup.cycle,
                MUNICIPAL,
                obligation=filing_subject(setup),
                actor=graph.actor,
            )
            link, _ = attach_filing(setup, request)
        else:
            request = requests.ensure_internal_request(setup.cycle, PERIOD)
    with migration_owner():
        try:
            with pytest.raises(IrreversibleError):
                executor.unapply_migration(old, migration)
        finally:
            if NEW not in executor.recorder.applied_migrations():
                executor.apply_migration(old.clone(), migration)
    retained = setup.reload(request)
    assert retained.purpose == (MUNICIPAL if staff_municipal else PERIOD)
    assert retained.created_by_id == (graph.actor.pk if staff_municipal else None)
    if link is not None:
        with tenant_context(graph.tenant.pk):
            assert EvidenceLink.objects.filter(pk=link.pk, request=request).exists()
