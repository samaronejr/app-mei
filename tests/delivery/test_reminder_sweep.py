"""Behavioral predictions authored before T39 implementation; not executed red."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from typing import TYPE_CHECKING

import pytest
from django.db import connection, transaction
from django.db.models.signals import post_save
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.clients.models import ClientAssignment, ClientContact
from apps.core.tenancy import tenant_context
from apps.cycles.completion_checks import completion_items
from apps.cycles.effects.executors import execute
from apps.cycles.evidence_index import capture_index
from apps.cycles.models import (
    CycleEvent,
    CycleState,
    EvidenceRequest,
    RequestState,
    ServiceCycle,
)
from apps.cycles.services.approvals import approve
from apps.cycles.services.completion import propose_reopen
from apps.cycles.services.proposed_transitions import propose_completion
from apps.delivery import journal, tasks
from apps.delivery.models import DeliveryAttempt, DeliveryEffect, MessageState
from apps.tenants.models import Membership, TenantRole
from tests.delivery import reminder_fixtures
from tests.delivery.reminder_fixtures import (
    NOW,
    allow_automatic,
    overdue_setup,
    reminders,
)
from tests.delivery.test_notice_dispatch import _runtime
from tests.ui.factories import add_member

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from apps.delivery.reminders import SweepCounts
    from tests.delivery.smtp import FakeSMTP
    from tests.portal.request_fixtures import PortalSetup

pytestmark = pytest.mark.django_db(transaction=True)
reminder_setup = reminder_fixtures.reminder_setup


def test_owner_form_persists_a_separate_automatic_rule_grant(
    reminder_setup: PortalSetup,
) -> None:
    # Given a published cycle with no tenant policy.
    setup = reminder_setup
    with tenant_context(setup.graph.tenant.pk):
        assert not reminders(setup)

    # When the actual owner explicitly acknowledges the automatic rule.
    policy = allow_automatic(setup)

    # Then this is distinct from every manual grant and GET does not reacknowledge.
    assert policy.acknowledgement_code == "notice-policy.automatic-reminders.v1"
    assert policy.acknowledged_by_id == setup.graph.actor.pk
    response = setup.firm().get(reverse("notice-policy"))
    assert response.context_data is not None
    assert response.context_data["form"].initial["automatic_reminders"] is True
    with tenant_context(setup.graph.tenant.pk):
        policy.refresh_from_db()
        assert policy.version == 1


def test_eligible_occurrence_queues_one_system_notice_and_sends_once(
    reminder_setup: PortalSetup, smtp_server: Callable[[str], FakeSMTP]
) -> None:
    # Given an exact automatic grant and an overdue request with one eligible contact.
    setup = reminder_setup
    policy = allow_automatic(setup)
    before_version = setup.request.version
    server = smtp_server("accept")

    # When the fixed-clock sweep repeats and the existing dispatcher repeats.
    first = tasks.sweep_reminders(clock=lambda: NOW)
    second = tasks.sweep_reminders(clock=lambda: NOW)
    assert server.received == []
    [message] = reminders(setup)
    tasks.dispatch_outbox()
    tasks.dispatch_outbox()

    # Then one natural occurrence has one immutable authority and one external effect.
    assert first["queued"] == 1
    assert second["queued"] == 0
    assert second["existing"] == 1
    assert message.notice_authorization_bytes is not None
    binding = json.loads(bytes(message.notice_authorization_bytes))
    assert binding["mode"] == "automatic"
    assert binding["system_operation"] == "sweep_reminders"
    assert binding["rule_version"] == 1
    assert binding["occurrence"] == 741362  # 2030-10-12, Gregorian ordinal.
    assert binding["expires_at"] == "2030-10-13T00:00:00-03:00"
    assert binding["basis"]["policy_id"] == str(policy.pk)
    assert message.created_by_id == policy.acknowledged_by_id
    assert setup.reload(setup.request).version == before_version
    with tenant_context(setup.graph.tenant.pk):
        message.refresh_from_db()
        assert message.state == MessageState.ACCEPTED
        assert DeliveryEffect.objects.filter(message=message).count() == 1
        [attempt] = DeliveryAttempt.objects.filter(
            message=message, error_code="enqueued"
        )
        assert attempt.actor_id is None
        [event] = CycleEvent.objects.filter(
            aggregate_id=message.pk, payload__code="enqueued"
        )
        assert event.actor_id is None
        assert not CycleEvent.objects.filter(type="request.reminded").exists()
    assert len(server.received) == 1
    # Journal files are named by random ids, so read order is not causal order.
    assert sorted(
        entry.outcome for entry in journal.read(setup.graph.tenant.pk)
    ) == sorted(["intent_started", MessageState.ACCEPTED])


def test_two_sweepers_observe_the_same_occurrence_but_commit_one_intent(
    reminder_setup: PortalSetup, smtp_server: Callable[[str], FakeSMTP]
) -> None:
    # Given signals installed before either real runtime worker selects the request.
    setup = reminder_setup
    allow_automatic(setup)
    selected, enqueued = Barrier(2, timeout=15), Event()
    server = smtp_server("accept")

    def observed_commit(
        sender: type[DeliveryAttempt], instance: DeliveryAttempt, **kwargs: object
    ) -> None:
        if kwargs.get("created") and instance.error_code == "enqueued":
            transaction.on_commit(enqueued.set)

    def after_selection(_tenant_id: UUID, _requests: tuple[UUID, ...]) -> None:
        selected.wait()

    def worker() -> SweepCounts:
        with connection.cursor() as cursor:
            cursor.execute("SET statement_timeout = '10s'")
        counts: SweepCounts = tasks.sweep_tenant_reminders(
            str(setup.graph.tenant.pk),
            clock=lambda: NOW,
            after_batch_selected=after_selection,
        )
        return counts

    post_save.connect(observed_commit, sender=DeliveryAttempt, weak=False)
    try:
        # When both workers reach their selection event before either enqueues.
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(_runtime, worker) for _ in range(2)]
            results = [future.result(timeout=20) for future in futures]
        assert enqueued.wait(5), "No committed outbox enqueue event"
    finally:
        post_save.disconnect(observed_commit, sender=DeliveryAttempt)
    tasks.dispatch_outbox()

    # Then the client fence and natural key converge, not a timing lucky assertion.
    assert sorted(result["queued"] for result in results) == [0, 1]
    assert sorted(result["existing"] for result in results) == [0, 1]
    assert len(reminders(setup)) == 1
    assert len(server.received) == 1
    with tenant_context(setup.graph.tenant.pk):
        assert DeliveryEffect.objects.count() == 1


def test_cursor_bounds_each_tenant_and_reaches_later_requests(
    reminder_setup: PortalSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a larger firm and a small firm; the test lowers the shipped batch bound.
    from apps.delivery import reminders as sweep  # noqa: PLC0415

    first, second = reminder_setup, overdue_setup("reminder-small")
    allow_automatic(first)
    allow_automatic(second)
    monkeypatch.setattr(sweep, "TENANT_REQUEST_LIMIT", 2)
    with tenant_context(first.graph.tenant.pk):
        for index in range(3):
            EvidenceRequest.objects.create(
                tenant=first.graph.tenant,
                client=first.graph.client,
                cycle=first.cycle,
                title=f"Request {index}",
                state=RequestState.REQUESTED,
                requested_at=NOW,
                due_on=first.request.due_on,
                created_by=first.graph.actor,
            )

    # When two sweeps visit both firms, advancing their tenant-private cursors.
    initial = tasks.sweep_reminders(clock=lambda: NOW)
    following = tasks.sweep_reminders(clock=lambda: NOW)

    # Then the small firm is never starved and later large-firm requests are reached.
    assert initial["evaluated"] == following["evaluated"] == 3
    assert initial["queued"] == 3
    assert following["queued"] == 2
    assert len(reminders(first)) == 4
    assert len(reminders(second)) == 1


def test_new_day_has_a_new_occurrence_without_catching_up_old_days(
    reminder_setup: PortalSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given yesterday's occurrence and an interruption lasting several days.
    setup = reminder_setup
    allow_automatic(setup)
    tasks.sweep_reminders(clock=lambda: NOW)
    later = NOW + timedelta(days=4)
    monkeypatch.setattr(timezone, "now", lambda: later)

    # When the worker resumes at the injected current time.
    result = tasks.sweep_reminders(clock=lambda: later)

    # Then only today's occurrence is created, with no catch-up flood.
    assert result["queued"] == 1
    assert len(reminders(setup)) == 2


@pytest.mark.parametrize("advance_days", [0, 4])
def test_contact_fanout_resumes_without_monopolizing_the_next_tenant(
    reminder_setup: PortalSetup,
    monkeypatch: pytest.MonkeyPatch,
    advance_days: int,
) -> None:
    # Given three real eligible contacts and a one-contact test batch limit.
    from apps.delivery import reminders as sweep  # noqa: PLC0415

    large, small = reminder_setup, overdue_setup("reminder-fanout-small")
    allow_automatic(large)
    allow_automatic(small)
    monkeypatch.setattr(sweep, "REQUEST_RECIPIENT_LIMIT", 1)
    contact_ids = {large.contact.pk}
    with tenant_context(large.graph.tenant.pk):
        for index in range(2):
            user = add_member(
                large.graph.tenant,
                f"contact-{index}@reminder-fanout.test",
                TenantRole.CLIENT_OWNER,
                client=large.graph.client,
            )
            contact = ClientContact.objects.create(
                tenant=large.graph.tenant,
                client=large.graph.client,
                name=f"Synthetic contact {index}",
                email=user.email,
            )
            contact_ids.add(contact.pk)

    # When bounded turns resume the partial request, including after an interruption.
    initial = tasks.sweep_reminders(clock=lambda: NOW)
    assert initial["queued"] == 2
    assert len(reminders(large)) == len(reminders(small)) == 1
    later = NOW + timedelta(days=advance_days)
    monkeypatch.setattr(timezone, "now", lambda: later)
    second = tasks.sweep_reminders(clock=lambda: later)
    third = tasks.sweep_reminders(clock=lambda: later)

    # Then later contacts are reached and the small firm always receives its turn.
    assert second["queued"] == 1 + int(advance_days > 0)
    assert third["queued"] == 1
    assert {message.contact_id for message in reminders(large)} == contact_ids
    assert len(reminders(large)) == 3


def test_an_approved_same_day_reopen_cannot_send_the_day_again(
    reminder_setup: PortalSetup,
    smtp_server: Callable[[str], FakeSMTP],
) -> None:
    """AXR1-039 review B1: the daily occurrence survives a cycle amendment."""
    setup = reminder_setup
    allow_automatic(setup)
    peer = smtp_server("accept")
    assert tasks.sweep_reminders(clock=lambda: NOW)["queued"] == 1
    tasks.dispatch_outbox()
    assert len(peer.received) == 1
    with tenant_context(setup.graph.tenant.pk):
        ClientAssignment.objects.create(
            tenant=setup.graph.tenant, client=setup.graph.client, user=setup.graph.staff
        )
        cycle = ServiceCycle.objects.get(pk=setup.cycle.pk)
        capture_index(setup.graph.actor, cycle.pk, expected_version=cycle.version)
        cycle.refresh_from_db()
        reasons = {
            item.key: "Pendência mantida para a revisão"
            for item in completion_items(cycle)
        }
        closure = propose_completion(
            setup.graph.staff,
            cycle.pk,
            to_state=CycleState.WITH_PENDING,
            reason=reasons,
            expected_version=cycle.version,
        )
        closure = approve(
            setup.graph.actor, closure.pk, expected_version=closure.version
        ).proposal
        assert execute(
            setup.graph.actor, closure.pk, expected_version=closure.version
        ).executed
        cycle.refresh_from_db()
        reviewer = User.objects.create_user(email="reopen-reviewer@example.test")
        Membership.objects.create(
            tenant=setup.graph.tenant, user=reviewer, role=TenantRole.OPERATIONS_ADMIN
        )
        reopening = propose_reopen(
            setup.graph.actor,
            cycle.pk,
            reason="Revisar a solicitação pendente",
            affected_scope=(setup.request.pk,),
            expected_version=cycle.version,
        )
        reopening = approve(
            reviewer, reopening.pk, expected_version=reopening.version
        ).proposal
        assert execute(
            setup.graph.actor, reopening.pk, expected_version=reopening.version
        ).executed
        cycle.refresh_from_db()
        assert cycle.amendment == 1
        assert setup.reload(setup.request).state == RequestState.REQUESTED

    result = tasks.sweep_reminders(clock=lambda: NOW)
    tasks.dispatch_outbox()

    assert result["queued"] == 0
    assert result["conflicts"] == 1
    assert len(peer.received) == 1
    with tenant_context(setup.graph.tenant.pk):
        accepted = DeliveryEffect.objects.filter(
            message__evidence_request=setup.request, outcome=MessageState.ACCEPTED
        ).count()
    assert accepted == 1
    assert len(reminders(setup)) == 1
