"""T17 coverage (`coverage_for`), the calendar producer and staff-recorded facts.

Every state the panel can name is exercised on real rows: unknown (nothing
asserted), not-applicable (regime says not owed), manual (staff or accepted
submission), pending_review (submission awaiting decision), stale and
refresh_failed (official heads), unknown_scope (unmapped service), verified
(official head inside the window) — and the three invariants the module exists
for: a calendar fact never implies a guia or a pagamento, a rejected or
retracted head counts as nothing, and the clock is injected so "stale" is a
function of `now`, not of the wall.

The race section drives the helper in tests/support_threads.py with
bounded `threading.Event` pairs — a blocker that never fires fails its own
timeout instead of sleeping the suite to death.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from threading import Event, Thread
from typing import Any
from uuid import UUID

import pytest
from django.db import transaction
from django.utils import timezone

from apps.clients.models import TaxProfile
from apps.core.tenancy import tenant_context
from apps.obligations.coverage import CoverageState, coverage_for
from apps.obligations.generator import generate_das_calendar
from apps.obligations.models import Obligation, SourceObservation
from apps.obligations.observations import (
    StaleObservationError,
    assert_fact,
    chain_heads,
    emit_calendar_assertions,
    lock_subjects,
    record_staff_fact,
    retract_staff_fact,
)
from tests.cycles.cycle_fixtures import PERIOD, CycleSetup, cycle_setup
from tests.support_threads import run_in_thread_as_runtime

DB = pytest.mark.django_db(transaction=True)


def _obligation(setup: CycleSetup) -> Obligation:
    # RLS scopes every read by app.current_tenant: outside the context the row
    # exists and cannot be seen, which reads as DoesNotExist, not as denied.
    with tenant_context(setup.tenant.pk):
        return Obligation.objects.get(
            client=setup.client, obligation_type_id="DAS", competence_month=PERIOD
        )


def _generate(setup: CycleSetup) -> Obligation:
    with tenant_context(setup.tenant.pk):
        generate_das_calendar(setup.client, starting_from=PERIOD)
    return _obligation(setup)


def _write_fact(  # noqa: PLR0913 - mirrors the assertion contract
    subject: Obligation,
    *,
    kind: str = "payment",
    period: date = PERIOD,
    source_kind: str = "staff_recorded",
    verification_status: str = "unknown",
    outcome: str = "recorded",
    supersedes: SourceObservation | None = None,
    observed_at: datetime | None = None,
) -> SourceObservation:
    return assert_fact(
        tenant_id=subject.tenant_id,
        client_id=subject.client_id,
        subject_id=subject.pk,
        kind=kind,
        period=period,
        source_kind=source_kind,
        observed_at=observed_at or timezone.now(),
        value={},
        verification_status=verification_status,
        outcome=outcome,
        supersedes=supersedes,
    )


def _item(
    setup: CycleSetup,
    *,
    engagement_id: UUID | None = None,
    now: datetime | None = None,
) -> Any:  # noqa: ANN401 - the CoverageItem contract, not a concrete type
    with tenant_context(setup.tenant.pk):
        items = coverage_for(
            setup.actor,
            setup.client,
            PERIOD,
            engagement_id=engagement_id,
            now=now,
        )
    return next(
        (row for row in items if row.engagement_id == setup.engagement.pk),
        items[0] if items else None,
    )


@DB
def test_no_obligation_row_is_unknown_not_not_applicable() -> None:
    # Given an active mei_mensal engagement whose generator never ran
    setup = cycle_setup("t17-cov-unknown")

    # Then the state is unknown: "não se aplica" would claim a DAS was not owed
    assert _item(setup).state == CoverageState.UNKNOWN


@DB
def test_non_mei_regime_marks_the_das_engagement_not_applicable() -> None:
    # Given an mei_mensal client whose regime at this competence is Simples
    setup = cycle_setup("t17-cov-na")
    with tenant_context(setup.tenant.pk):
        profile = TaxProfile.objects.get(client=setup.client)
        profile.regime = "simples"
        profile.save(update_fields=["regime"])

    # Then the DAS coverage is positively not applicable, not merely unknown
    assert _item(setup).state == CoverageState.NOT_APPLICABLE


@DB
def test_a_profile_gap_at_the_period_is_unknown() -> None:
    # Given a generated DAS whose client's claim starts only next month
    setup = cycle_setup("t17-cov-gap")
    _generate(setup)
    with tenant_context(setup.tenant.pk):
        profile = TaxProfile.objects.get(client=setup.client)
        profile.valid_from = PERIOD + timedelta(days=32)
        profile.save(update_fields=["valid_from"])

    # Then the timeline gap answers unknown — a hole is not inapplicability
    assert _item(setup).state == CoverageState.UNKNOWN


@DB
def test_calendar_producer_writes_once_and_replays_cleanly() -> None:
    # Given a flagged tenant the generator just produced a DAS row for
    setup = cycle_setup("t17-producer")
    subject = _generate(setup)
    with tenant_context(setup.tenant.pk):
        rows = SourceObservation.objects.filter(
            subject_ref=subject, kind="calendar_scheduled"
        )

        # Then exactly one verified deterministic assertion exists, scheduled
        # no later than two days before the deadline it asserts
        assert rows.count() == 1
        head = rows.get()
        assert head.source_kind == "deterministic_rule"
        assert head.verification_status == "verified"
        assert head.observed_at.date() <= min(
            subject.nominal_due_date, subject.resolved_due_date
        ) - timedelta(days=2)
        # And the calendar fact asserts the schedule only — never guia/payment
        assert _item(setup).facets == {
            "calendar": CoverageState.VERIFIED,
            "guide": CoverageState.UNKNOWN,
            "payment": CoverageState.UNKNOWN,
        }
        assert _item(setup).state == CoverageState.UNKNOWN

        # And a replay is a no-op, not a second root
        assert emit_calendar_assertions([subject]) == 0
        assert (
            SourceObservation.objects.filter(
                subject_ref=subject, kind="calendar_scheduled"
            ).count()
            == 1
        )


@DB
def test_a_changed_schedule_supersedes_the_calendar_head() -> None:
    # Given an obligation whose resolved date moved after the assertion landed
    setup = cycle_setup("t17-refresh")
    subject = _generate(setup)
    with tenant_context(setup.tenant.pk):
        subject.resolved_due_date = subject.resolved_due_date + timedelta(days=1)
        subject.save(update_fields=["resolved_due_date"])

        # When the producer runs again it supersedes, it never forks
        assert emit_calendar_assertions([subject]) == 1
        assert (
            SourceObservation.objects.filter(
                subject_ref=subject, kind="calendar_scheduled"
            ).count()
            == 2
        )
        heads = [
            row
            for row in SourceObservation.objects.filter(
                subject_ref=subject, kind="calendar_scheduled"
            )
            if not SourceObservation.objects.filter(supersedes=row).exists()
        ]
        assert len(heads) == 1
        assert heads[0].value_json["resolved_due_date"] == (
            subject.resolved_due_date.isoformat()
        )


@DB
def test_staff_fact_is_manual_and_correctable() -> None:
    # Given a generated DAS and a staff-recorded payment against it
    setup = cycle_setup("t17-manual")
    subject = _generate(setup)
    with tenant_context(setup.tenant.pk):
        head = record_staff_fact(
            setup.actor, subject_id=subject.pk, kind="payment", period=PERIOD
        )

    # Then coverage is manual and names the head a correction must target
    item = _item(setup)
    assert item.state == CoverageState.MANUAL
    assert item.head_id == head.pk
    assert item.correctable


@DB
def test_a_client_submission_pending_review_reads_as_pending() -> None:
    # Given a receipt observation the firm has not decided on yet
    setup = cycle_setup("t17-pending")
    subject = _generate(setup)
    with tenant_context(setup.tenant.pk):
        _write_fact(
            subject,
            source_kind="client_submitted",
            verification_status="pending_review",
            outcome="submitted",
        )

    # Then the item is pending_review — informed, not verified, not unknown
    assert _item(setup).state == CoverageState.PENDING_REVIEW


@DB
def test_an_official_verified_head_inside_the_window_is_verified() -> None:
    # Given an official_api verification three days old
    setup = cycle_setup("t17-verified")
    subject = _generate(setup)
    now = timezone.now()
    with tenant_context(setup.tenant.pk):
        _write_fact(
            subject,
            source_kind="official_api",
            verification_status="verified",
            outcome="accepted",
            observed_at=now - timedelta(days=3),
        )

        # Then inside the seven-day window the item is verified
        assert _item(setup, now=now).state == CoverageState.VERIFIED


@DB
def test_the_same_verified_head_is_stale_outside_the_window() -> None:
    # Given an official_api verification eight days old
    setup = cycle_setup("t17-stale")
    subject = _generate(setup)
    with tenant_context(setup.tenant.pk):
        _write_fact(
            subject,
            source_kind="official_api",
            verification_status="verified",
            outcome="accepted",
            observed_at=timezone.now() - timedelta(days=8),
        )

        # Then the same row reads stale — freshness is a function of `now`
        assert _item(setup).state == CoverageState.STALE


@DB
def test_a_failed_refresh_never_counts_as_knowledge() -> None:
    # Given an official refresh that failed after the last good read
    setup = cycle_setup("t17-refreshfail")
    subject = _generate(setup)
    with tenant_context(setup.tenant.pk):
        _write_fact(
            subject,
            source_kind="official_api",
            verification_status="refresh_failed",
            outcome="refresh_failed",
        )

        # Then the item names the failure — it cannot borrow a verified look
        assert _item(setup).state == CoverageState.REFRESH_FAILED


@DB
def test_a_rejected_receipt_head_supersedes_to_unknown() -> None:
    # Given a submission whose review rejected it as payment evidence
    setup = cycle_setup("t17-reject")
    subject = _generate(setup)
    with tenant_context(setup.tenant.pk):
        submission = _write_fact(
            subject,
            source_kind="client_submitted",
            verification_status="pending_review",
            outcome="submitted",
        )
        _write_fact(
            subject,
            source_kind="staff_recorded",
            verification_status="unsupported",
            outcome="rejected",
            supersedes=submission,
        )

        # Then the chain's head asserts nothing — the item is unknown again
        assert _item(setup).state == CoverageState.UNKNOWN


@DB
def test_retraction_is_a_superseding_unsupported_head() -> None:
    # Given a manual fact that is later retracted
    setup = cycle_setup("t17-retract")
    subject = _generate(setup)
    with tenant_context(setup.tenant.pk):
        head = record_staff_fact(
            setup.actor, subject_id=subject.pk, kind="payment", period=PERIOD
        )
        assert _item(setup).state == CoverageState.MANUAL
        retract_staff_fact(
            setup.actor,
            subject_id=subject.pk,
            kind="payment",
            period=PERIOD,
            expected_head_id=head.pk,
        )

        # Then the retraction, not deletion, is what removes the knowledge
        assert _item(setup).state == CoverageState.UNKNOWN


@DB
def test_correction_of_a_stale_head_is_refused() -> None:
    # Given a chain whose head already moved past the form's snapshot
    setup = cycle_setup("t17-stalehead")
    subject = _generate(setup)
    with tenant_context(setup.tenant.pk):
        first = record_staff_fact(
            setup.actor, subject_id=subject.pk, kind="payment", period=PERIOD
        )
        record_staff_fact(
            setup.actor,
            subject_id=subject.pk,
            kind="payment",
            period=PERIOD,
            expected_head_id=first.pk,
        )

        # Then a second correction naming the superseded row is refused
        with pytest.raises(StaleObservationError):
            record_staff_fact(
                setup.actor,
                subject_id=subject.pk,
                kind="payment",
                period=PERIOD,
                expected_head_id=first.pk,
            )


@DB
def test_another_engagement_service_kind_has_unknown_scope() -> None:
    # Given an engagement whose service kind has no obligation mapping
    setup = cycle_setup("t17-scope")
    with tenant_context(setup.tenant.pk):
        setup.engagement.service_kind = "folha"
        setup.engagement.save(update_fields=["service_kind"])

    # Then the item is inert: neither applicable nor claimable as covered
    assert _item(setup).state == CoverageState.UNKNOWN_SCOPE


@DB
def test_coverage_does_not_leak_across_competences() -> None:
    # Given a payment fact asserted for September only
    setup = cycle_setup("t17-months")
    subject = _generate(setup)
    with tenant_context(setup.tenant.pk):
        record_staff_fact(
            setup.actor, subject_id=subject.pk, kind="payment", period=PERIOD
        )
        items = coverage_for(setup.actor, setup.client, date(2026, 10, 1))

    # Then October's engagement exists but carries none of September's knowledge
    assert items[0].state == CoverageState.UNKNOWN


@DB
def test_race_two_generators_write_one_assertion() -> None:
    """Both emitters take the subject lock; the loser sees the winner's head."""
    # Given a generated DAS whose calendar assertion already exists
    setup = cycle_setup("t17-race-cal")
    with tenant_context(setup.tenant.pk):
        generate_das_calendar(setup.client, starting_from=PERIOD)
        subject = Obligation.objects.get(
            client=setup.client,
            obligation_type_id="DAS",
            competence_month=PERIOD,
        )
        assert emit_calendar_assertions([subject]) == 0

    # When two emitters race, both find the head and both write nothing
    def emit() -> int:
        return emit_calendar_assertions([Obligation.objects.get(pk=subject.pk)])

    first = run_in_thread_as_runtime(setup.tenant.pk, emit)
    second = run_in_thread_as_runtime(setup.tenant.pk, emit)
    assert first == 0
    assert second == 0
    assert _head_count(setup, subject) == 1


def _head_count(setup: CycleSetup, subject: Obligation) -> int:
    with tenant_context(setup.tenant.pk):
        return SourceObservation.objects.filter(
            subject_ref=subject, kind="calendar_scheduled"
        ).count()


@DB
def test_race_concurrent_corrections_cannot_fork_the_chain() -> None:
    """Two superseding writes racing one head never leave it with two heirs.

    `record_staff_fact` re-reads the capability tables, which `app_test` may
    not touch — the concurrent writers here run the lock-and-head-check the
    service performs, the same seam the effect manifest's races exercise.
    """
    # Given a manual head two writers both want to supersede at once
    setup = cycle_setup("t17-race-correct")
    with tenant_context(setup.tenant.pk):
        generate_das_calendar(setup.client, starting_from=PERIOD)
        subject = Obligation.objects.get(
            client=setup.client,
            obligation_type_id="DAS",
            competence_month=PERIOD,
        )
        head = _write_fact(subject)
    gate = Event()
    outcomes: dict[str, str] = {}

    def correct(tag: str) -> None:
        def run() -> None:
            gate.wait(timeout=10)
            with transaction.atomic():
                list(lock_subjects([subject.pk]))
                heads = [row.pk for row in chain_heads(subject.pk, "payment", PERIOD)]
                if heads != [head.pk]:
                    outcomes[tag] = "stale"
                    return
                assert_fact(
                    tenant_id=subject.tenant_id,
                    client_id=subject.client_id,
                    subject_id=subject.pk,
                    kind="payment",
                    period=PERIOD,
                    source_kind="staff_recorded",
                    observed_at=timezone.now(),
                    value={},
                    verification_status="unknown",
                    outcome="corrected",
                    supersedes=SourceObservation.objects.get(pk=head.pk),
                )
                outcomes[tag] = "written"

        try:
            run_in_thread_as_runtime(setup.tenant.pk, run)
        except BaseException as error:  # noqa: BLE001 - counted below
            outcomes[tag] = f"error: {error!r}"

    # Both threads run the same lock-first, check-second writer the service
    # performs; the losing one reads the moved chain and refuses.

    with ThreadPoolExecutor(max_workers=2) as pool:
        alpha = pool.submit(correct, "alpha")
        beta = pool.submit(correct, "beta")
        gate.set()
        alpha.result(timeout=30)
        beta.result(timeout=30)

    # Then exactly one writer superseded the head; the loser read the moved
    # chain under the lock and refused rather than forking a second heir
    assert sorted(outcomes.values()) == ["stale", "written"], outcomes
    with tenant_context(setup.tenant.pk):
        assert SourceObservation.objects.filter(supersedes_id=head.pk).count() == 1


@DB
def test_race_observation_vs_locked_subject_returns_after_commit() -> None:
    """A head re-read under a held subject lock sees the committed successor."""
    # Given a manual head and a transaction holding the subject lock
    setup = cycle_setup("t17-race-lock")
    with tenant_context(setup.tenant.pk):
        generate_das_calendar(setup.client, starting_from=PERIOD)
        subject = Obligation.objects.get(
            client=setup.client,
            obligation_type_id="DAS",
            competence_month=PERIOD,
        )
        head = _write_fact(subject)
    held = Event()
    go = Event()
    outcome: dict[str, Any] = {}

    def holder() -> None:
        with transaction.atomic():
            list(lock_subjects([subject.pk]))
            held.set()
            # The go event is the release: whatever the contender does next,
            # this lock is out of its way and never silently held past it.
            assert go.wait(timeout=30)

    def contender() -> None:
        # Waits on the subject lock; the holder's committed chain is what the
        # re-read sees — the successor write lands against it, never beside it.
        with transaction.atomic():
            list(lock_subjects([subject.pk]))
            assert [row.pk for row in chain_heads(subject.pk, "payment", PERIOD)] == [
                head.pk
            ]
            outcome["row"] = assert_fact(
                tenant_id=subject.tenant_id,
                client_id=subject.client_id,
                subject_id=subject.pk,
                kind="payment",
                period=PERIOD,
                source_kind="staff_recorded",
                observed_at=timezone.now(),
                value={},
                verification_status="unknown",
                outcome="corrected",
                supersedes=SourceObservation.objects.get(pk=head.pk),
            )

    keeper = Thread(
        target=lambda: run_in_thread_as_runtime(setup.tenant.pk, holder),
        daemon=True,
    )
    rival = Thread(
        target=lambda: run_in_thread_as_runtime(setup.tenant.pk, contender),
        daemon=True,
    )
    keeper.start()
    assert held.wait(timeout=10)
    rival.start()
    # Give the contender its head start at the lock, then let the holder go.
    go.set()
    rival.join(timeout=30)
    keeper.join(timeout=10)

    # Then the write that ran behind the lock landed as the head's successor
    assert "error" not in outcome, outcome.get("error")
    with tenant_context(setup.tenant.pk):
        fresh = SourceObservation.objects.get(pk=outcome["row"].pk)
        assert fresh.supersedes_id == head.pk


@DB
def test_now_is_an_injected_clock_not_a_wall_read() -> None:
    """The same rows answer differently at different evaluation instants."""
    # Given an official verification one hour inside its window
    setup = cycle_setup("t17-clock")
    subject = _generate(setup)
    moment = timezone.now()
    with tenant_context(setup.tenant.pk):
        _write_fact(
            subject,
            source_kind="official_api",
            verification_status="verified",
            outcome="accepted",
            observed_at=moment - timedelta(days=6, hours=23),
        )
        inside = _item(setup, now=moment)
        outside = _item(setup, now=moment + timedelta(seconds=3601))

    # Then the boundary is the injected `now`, not the wall clock
    assert inside.state == CoverageState.VERIFIED
    assert outside.state == CoverageState.STALE


@DB
def test_unrelated_observation_kinds_never_count_as_payment_coverage() -> None:
    # Given a verified official filing assertion and no payment claims
    setup = cycle_setup("t17-kind")
    subject = _generate(setup)
    with tenant_context(setup.tenant.pk):
        _write_fact(
            subject,
            kind="filing",
            source_kind="official_api",
            verification_status="verified",
            outcome="accepted",
        )

    # Then a filing can never stand in for payment knowledge
    item = _item(setup)
    assert item.state == CoverageState.UNKNOWN
    assert item.facets["payment"] == CoverageState.UNKNOWN
