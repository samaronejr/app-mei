"""New observations invalidate an old closure; source contention never deadlocks it."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.cycles.effects.executors import execute
from apps.cycles.errors import VersionConflict
from apps.cycles.evidence_index import capture_index, index_current
from apps.cycles.export_records import EvidenceIndex
from apps.cycles.models import ActionExecution, CycleEvent, CycleState, ProposalState
from apps.cycles.services.requests import RequestFields, create_request
from apps.obligations.generator import generate_das_calendar
from apps.obligations.models import Obligation, SourceObservation
from tests.cycles.completion_fixtures import (
    CompletionSetup,
    approved_completion,
    completion_setup,
)
from tests.support_threads import run_in_thread_as_runtime

pytestmark = pytest.mark.django_db(transaction=True)


def bind_subject(setup: CompletionSetup) -> Obligation:
    generate_das_calendar(setup.graph.client, starting_from=setup.cycle.period_start)
    subject = Obligation.objects.get(
        client=setup.graph.client, competence_month=setup.cycle.period_start
    )
    create_request(
        setup.graph.actor,
        setup.cycle.pk,
        RequestFields(
            "Comprovante opcional",
            required=False,
            kind="comprovante",
            obligation_id=subject.pk,
        ),
        expected_version=setup.cycle.version,
    )
    setup.cycle.refresh_from_db()
    return subject


def test_new_observation_invalidates_approval_even_after_index_regeneration() -> None:
    setup = completion_setup("completion-new-source")
    with tenant_context(setup.graph.tenant.pk):
        subject = bind_subject(setup)
        proposal = approved_completion(setup)
        frozen = bytes(proposal.manifest_bytes)
        Obligation.objects.select_for_update(no_key=True).get(pk=subject.pk)
        SourceObservation.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            subject_ref=subject,
            period=setup.cycle.period_start,
            kind="payment",
            source_kind="staff_recorded",
            verification_status="unknown",
            observed_at=timezone.now(),
        )
        setup.cycle.refresh_from_db()
        capture_index(
            setup.graph.actor, setup.cycle.pk, expected_version=setup.cycle.version
        )
        setup.cycle.refresh_from_db()
        assert index_current(setup.cycle)
        result = execute(
            setup.graph.actor, proposal.pk, expected_version=proposal.version
        )
        assert not result.executed
        proposal.refresh_from_db()
        assert proposal.state == ProposalState.INVALIDATED
        assert bytes(proposal.manifest_bytes) == frozen
        assert not ActionExecution.objects.filter(proposal=proposal).exists()
        setup.cycle.refresh_from_db()
        assert setup.cycle.state == CycleState.EXECUTION


def test_subject_first_writer_causes_retryable_index_conflict_without_writes() -> None:
    setup = completion_setup("completion-source-fence")
    with tenant_context(setup.graph.tenant.pk):
        subject = bind_subject(setup)
        before = (EvidenceIndex.objects.count(), CycleEvent.objects.count())
    locked, release = Event(), Event()

    def hold_subject() -> None:
        Obligation.objects.select_for_update(no_key=True).get(pk=subject.pk)
        locked.set()
        assert release.wait(timeout=30), "The source fence was not released."

    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(
            run_in_thread_as_runtime, setup.graph.tenant.pk, hold_subject
        )
        try:
            assert locked.wait(timeout=30), (
                "The independent writer did not acquire the fence."
            )
            with tenant_context(setup.graph.tenant.pk):
                with pytest.raises(VersionConflict):
                    capture_index(
                        setup.graph.actor,
                        setup.cycle.pk,
                        expected_version=setup.cycle.version,
                    )
                assert (
                    EvidenceIndex.objects.count(),
                    CycleEvent.objects.count(),
                ) == before
        finally:
            release.set()
        writer.result(timeout=30)
    with tenant_context(setup.graph.tenant.pk):
        index = capture_index(
            setup.graph.actor, setup.cycle.pk, expected_version=setup.cycle.version
        )
        assert index_current(setup.cycle)
        assert index.cycle_id == setup.cycle.pk


def test_other_kind_successor_does_not_remove_a_current_completion_source() -> None:
    setup = completion_setup("completion-source-head")
    with tenant_context(setup.graph.tenant.pk):
        subject = bind_subject(setup)
        existing = set(
            SourceObservation.objects.filter(
                subject_ref=subject, period=setup.cycle.period_start
            ).values_list("pk", flat=True)
        )
        Obligation.objects.select_for_update(no_key=True).get(pk=subject.pk)
        payment = SourceObservation.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            subject_ref=subject,
            period=setup.cycle.period_start,
            kind="payment",
            source_kind="staff_recorded",
            verification_status="unknown",
            observed_at=timezone.now(),
        )
        guide = SourceObservation.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            subject_ref=subject,
            period=setup.cycle.period_start,
            kind="guide_issued",
            source_kind="staff_recorded",
            verification_status="unknown",
            observed_at=timezone.now(),
            supersedes=payment,
        )
        proposal = approved_completion(setup)
        assert set(proposal.input_spec["observations"]) == {
            str(pk) for pk in existing
        } | {
            str(payment.pk),
            str(guide.pk),
        }
