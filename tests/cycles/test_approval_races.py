"""Real runtime sessions prove the exact fences, without sleeps or mock authority."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Event

import pytest
from django.core.exceptions import PermissionDenied
from django.core.files.storage import default_storage
from django.db import OperationalError, connection, transaction
from django.utils import timezone
from psycopg.errors import LockNotAvailable
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.authz.grants import set_review_capability
from apps.core.tenancy import tenant_context
from apps.cycles.effects.build import BuildError
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.extraction.service import (
    claim_extraction,
    record_extraction,
    run_extraction,
)
from apps.cycles.models import (
    ActionProposal,
    Approval,
    DocumentIntake,
    EvidenceLink,
    ProposalState,
)
from apps.cycles.scanning import (
    FakeScanner,
    claim_scan,
    promote,
    record_verdict,
    run_scan,
)
from apps.cycles.services.approvals import (
    approve,
    configure_approval_policy,
    propose,
    revalidate,
)
from apps.obligations.generator import generate_das_calendar
from apps.obligations.models import Obligation, SourceObservation
from apps.tenants.models import Membership, Tenant
from tests.cycles.approval_fixtures import (
    SIGNAL_TIMEOUT,
    ApprovalGraph,
    LockSignal,
    approval_graph,
)
from tests.cycles.document_fixtures import scan_clean, submit
from tests.portal.request_fixtures import PortalSetup, portal_setup
from tests.support_threads import run_in_thread_as_runtime

pytestmark = pytest.mark.django_db(transaction=True)


def _observation(setup: ApprovalGraph) -> SourceObservation:
    generate_das_calendar(setup.graph.client, starting_from=setup.cycle.period_start)
    subject = Obligation.objects.get(
        client=setup.graph.client, competence_month=setup.cycle.period_start
    )
    with transaction.atomic():
        Obligation.objects.select_for_update(no_key=True).get(pk=subject.pk)
        return SourceObservation.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            subject_ref=subject,
            period=setup.cycle.period_start,
            kind="payment",
            source_kind="staff_recorded",
            verification_status="unknown",
            observed_at=timezone.now(),
        )


def _with_source(setup: ApprovalGraph, source: SourceObservation) -> ActionProposal:
    proposal = propose(
        setup.graph.staff,
        setup.cycle.pk,
        ProposalDraft(
            replace(
                ProposalInput.from_data(setup.proposal.input_spec),
                observation_ids=(source.pk,),
            ),
            setup.proposal.expires_at,
            previous_id=setup.proposal.pk,
            previous_version=1,
        ),
        expected_version=setup.cycle.version,
    )
    approve(setup.graph.actor, proposal.pk, expected_version=1)
    return proposal


def _successor(source: SourceObservation) -> SourceObservation:
    """Exercise the producer contract: subject fence BEFORE any head lookup."""
    with transaction.atomic():
        Obligation.objects.select_for_update(no_key=True).get(pk=source.subject_ref_id)
        assert not SourceObservation.objects.filter(supersedes=source).exists()
        return SourceObservation.objects.create(
            tenant_id=source.tenant_id,
            client_id=source.client_id,
            subject_ref_id=source.subject_ref_id,
            period=source.period,
            kind=source.kind,
            source_kind="staff_recorded",
            verification_status="unknown",
            observed_at=source.observed_at - timedelta(days=1),
            supersedes=source,
        )


def test_revalidation_holds_the_subject_fence_until_its_decision_commits() -> None:
    setup = approval_graph("approval-subject-first")
    with tenant_context(setup.graph.tenant.pk):
        source = _observation(setup)
        proposal = _with_source(setup, source)
    held, release = Event(), Event()

    def reader() -> bool:
        with connection.execute_wrapper(
            LockSignal("obligations_obligation", held, release, after=True)
        ):
            return revalidate(setup.graph.actor, proposal.pk).executable

    def probe() -> None:
        assert Obligation.objects.filter(pk=source.subject_ref_id).exists()
        with transaction.atomic():
            Obligation.objects.select_for_update(no_key=True, nowait=True).get(
                pk=source.subject_ref_id
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        reading = pool.submit(run_in_thread_as_runtime, setup.graph.tenant.pk, reader)
        try:
            assert held.wait(SIGNAL_TIMEOUT)
            with pytest.raises(OperationalError) as blocked:
                run_in_thread_as_runtime(setup.graph.tenant.pk, probe)
            assert isinstance(blocked.value.__cause__, LockNotAvailable)
        finally:
            release.set()
        assert reading.result(timeout=SIGNAL_TIMEOUT)
    run_in_thread_as_runtime(setup.graph.tenant.pk, lambda: _successor(source))
    with tenant_context(setup.graph.tenant.pk):
        assert not revalidate(setup.graph.actor, proposal.pk).executable


def test_writer_first_is_seen_after_the_subject_fence_not_the_discovery_read() -> None:
    setup = approval_graph("approval-source-writer-first")
    with tenant_context(setup.graph.tenant.pk):
        source = _observation(setup)
        proposal = _with_source(setup, source)
    entering = Event()

    def reader() -> bool:
        with connection.execute_wrapper(LockSignal("obligations_obligation", entering)):
            return revalidate(setup.graph.actor, proposal.pk).executable

    with ThreadPoolExecutor(max_workers=1) as pool:
        with tenant_context(setup.graph.tenant.pk), transaction.atomic():
            _successor(source)
            reading = pool.submit(
                run_in_thread_as_runtime, setup.graph.tenant.pk, reader
            )
            assert entering.wait(SIGNAL_TIMEOUT)
        assert not reading.result(timeout=SIGNAL_TIMEOUT)
    with tenant_context(setup.graph.tenant.pk):
        proposal.refresh_from_db()
        assert proposal.state == ProposalState.INVALIDATED


def test_approver_grant_expiry_is_rechecked_after_the_last_blocking_source_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = approval_graph("approval-expiry-during-source-lock")
    clock = [timezone.now()]
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    with tenant_context(setup.graph.tenant.pk):
        source = _observation(setup)
        member = Membership.objects.for_user(setup.graph.actor).get(
            tenant=setup.graph.tenant, user=setup.graph.staff, client__isnull=True
        )
        grant = set_review_capability(
            actor=setup.graph.actor,
            membership=member,
            enabled=True,
            expected_version=member.review_grant_version,
        )
        assert grant is not None
        grant.expires_at = clock[0] + timedelta(minutes=1)
        grant.save(update_fields=["expires_at"])
        proposal = propose(
            setup.graph.actor,
            setup.cycle.pk,
            ProposalDraft(
                replace(
                    ProposalInput.from_data(setup.proposal.input_spec),
                    observation_ids=(source.pk,),
                ),
                setup.proposal.expires_at,
                previous_id=setup.proposal.pk,
                previous_version=1,
            ),
            expected_version=setup.cycle.version,
        )
    entering = Event()

    def decision() -> str:
        with connection.execute_wrapper(LockSignal("obligations_obligation", entering)):
            try:
                approve(setup.graph.staff, proposal.pk, expected_version=1)
            except PermissionDenied:
                return "denied"
            return "approved"

    with ThreadPoolExecutor(max_workers=1) as pool:
        with tenant_context(setup.graph.tenant.pk), transaction.atomic():
            Obligation.objects.select_for_update(no_key=True).get(
                pk=source.subject_ref_id
            )
            deciding = pool.submit(
                run_in_thread_as_runtime, setup.graph.tenant.pk, decision
            )
            assert entering.wait(SIGNAL_TIMEOUT)
            clock[0] += timedelta(minutes=2)
        assert deciding.result(timeout=SIGNAL_TIMEOUT) == "denied"
    with tenant_context(setup.graph.tenant.pk):
        proposal.refresh_from_db()
        assert proposal.state == ProposalState.AWAITING
        assert not Approval.objects.filter(proposal=proposal).exists()


def _sole(setup: ApprovalGraph) -> ActionProposal:
    setup.graph.staff.is_active = False
    setup.graph.staff.save(update_fields=["is_active"])
    configure_approval_policy(
        setup.graph.actor,
        expected_version=1,
        allow_self_approval=True,
        acknowledged=True,
    )
    return propose(
        setup.graph.actor,
        setup.cycle.pk,
        ProposalDraft(
            ProposalInput.from_data(setup.proposal.input_spec),
            setup.proposal.expires_at,
            previous_id=setup.proposal.pk,
            previous_version=1,
        ),
        expected_version=setup.cycle.version,
    )


def _probe_policy(setup: ApprovalGraph) -> None:
    with transaction.atomic():
        Tenant.objects.select_for_update(no_key=True, nowait=True).get(
            pk=setup.graph.tenant.pk
        )


def test_user_activation_first_fences_the_fresh_count_before_self_approval() -> None:
    setup = approval_graph("approval-activation-first")
    with tenant_context(setup.graph.tenant.pk):
        proposal = _sole(setup)
    entering = Event()

    def decision() -> str:
        with connection.execute_wrapper(LockSignal("tenants_tenant", entering)):
            return approve(
                setup.graph.actor,
                proposal.pk,
                expected_version=1,
                self_approval_acknowledged=True,
            ).proposal.state

    with ThreadPoolExecutor(max_workers=1) as pool:
        with tenant_context(setup.graph.tenant.pk), transaction.atomic():
            setup.graph.staff.is_active = True
            setup.graph.staff.save(update_fields=["is_active"])
            with pytest.raises(OperationalError) as blocked:
                run_in_thread_as_runtime(
                    setup.graph.tenant.pk, lambda: _probe_policy(setup)
                )
            assert isinstance(blocked.value.__cause__, LockNotAvailable)
            deciding = pool.submit(
                run_in_thread_as_runtime, setup.graph.tenant.pk, decision
            )
            assert entering.wait(SIGNAL_TIMEOUT)
        assert deciding.result(timeout=SIGNAL_TIMEOUT) == ProposalState.INVALIDATED
    with tenant_context(setup.graph.tenant.pk):
        assert not Approval.objects.filter(proposal=proposal).exists()


def test_approval_first_fences_user_activation_then_later_revalidation_refuses() -> (
    None
):
    setup = approval_graph("approval-before-activation")
    with tenant_context(setup.graph.tenant.pk):
        proposal = _sole(setup)
    entered = Event()

    def activate() -> None:
        with transaction.atomic():
            user = User.objects.select_for_update(no_key=True).get(
                pk=setup.graph.staff.pk
            )
            entered.set()
            user.is_active = True
            user.save(update_fields=["is_active"])

    with ThreadPoolExecutor(max_workers=1) as pool:
        with tenant_context(setup.graph.tenant.pk), transaction.atomic():
            result = approve(
                setup.graph.actor,
                proposal.pk,
                expected_version=1,
                self_approval_acknowledged=True,
            )
            assert result.executable
            with pytest.raises(OperationalError) as blocked:
                run_in_thread_as_runtime(
                    setup.graph.tenant.pk, lambda: _probe_policy(setup)
                )
            assert isinstance(blocked.value.__cause__, LockNotAvailable)
            activation = pool.submit(
                run_in_thread_as_runtime, setup.graph.tenant.pk, activate
            )
            assert entered.wait(SIGNAL_TIMEOUT)
        activation.result(timeout=SIGNAL_TIMEOUT)
    with tenant_context(setup.graph.tenant.pk):
        assert not revalidate(setup.graph.actor, proposal.pk).executable
        assert Approval.objects.filter(proposal=proposal).count() == 1


def _prepare_finalizer(
    setup: PortalSetup, intake: DocumentIntake, phase: str
) -> Callable[[], str]:
    if phase == "scan":
        scan_ticket = claim_scan(setup.graph.tenant.pk, intake.pk)
        assert scan_ticket is not None
        verdict = run_scan(scan_ticket, FakeScanner(), default_storage)
        promote(scan_ticket, default_storage)
        return lambda: record_verdict(scan_ticket, verdict, now=timezone.now())
    scan_clean(setup, intake)
    extraction_ticket = claim_extraction(setup.graph.tenant.pk, intake.pk)
    assert extraction_ticket is not None
    extracted = run_extraction(extraction_ticket, default_storage)
    return lambda: record_extraction(extraction_ticket, extracted, now=timezone.now())


@pytest.mark.parametrize("phase", ["scan", "extraction"])
def test_builder_does_not_wait_on_intake_or_document_while_holding_cycle(
    settings: SettingsWrapper,
    tmp_path: Path,
    phase: str,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    setup = portal_setup("approval-extraction-race")
    intake = submit(setup, "nfe_emitente_cliente.xml")
    finish = _prepare_finalizer(setup, intake, phase)
    with tenant_context(setup.graph.tenant.pk):
        link = EvidenceLink.objects.get(document_id=intake.document_id)
        setup.cycle.refresh_from_db()
    held, release = Event(), Event()

    def finalize() -> str:
        with connection.execute_wrapper(
            LockSignal("cycles_servicecycle", held, release)
        ):
            return finish()

    def preview() -> None:
        with pytest.raises(BuildError, match="document_not_reviewed"):
            propose(
                setup.graph.actor,
                setup.cycle.pk,
                ProposalDraft(
                    ProposalInput(
                        action_kind=ActionKind.EMAIL_NOTICE,
                        step_key="extract-race",
                        recipient_ids=(setup.contact.pk,),
                        document_link_ids=(link.pk,),
                        subject="Conferir nota",
                        body="Confira o arquivo selecionado.",
                    ),
                    timezone.now() + timedelta(days=1),
                ),
                expected_version=setup.cycle.version,
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        finalizing = pool.submit(
            run_in_thread_as_runtime, setup.graph.tenant.pk, finalize
        )
        try:
            assert held.wait(SIGNAL_TIMEOUT)
            run_in_thread_as_runtime(setup.graph.tenant.pk, preview)
        finally:
            release.set()
        expected = "verificado" if phase == "scan" else "classificado"
        assert finalizing.result(timeout=SIGNAL_TIMEOUT) == expected
    with tenant_context(setup.graph.tenant.pk):
        assert DocumentIntake.objects.get(pk=intake.pk).state == expected
        assert not ActionProposal.objects.exists()
