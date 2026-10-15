"""AXR1-044: the delivery projection types what each append-only receipt did.

A late cancellation or failure after acceptance, a genuine provider rejection, an
uncertain outcome, a superseded fence and an explicit client receipt each reach the
view model with a typed effect. Assertions read the typed fields, never the prose.
"""

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.models import ClientPublication
from apps.cycles.workspace import TimelineEntry
from apps.delivery.contracts import Reply
from apps.delivery.gateway import send
from apps.delivery.models import DeliveryAttempt, MessageState, OutboundMessage
from apps.delivery.services import claim_batch, finalize
from apps.delivery.timeline import (
    DeliveryObservation,
    ObservationEffect,
    message_history,
    read_timeline,
)
from tests.cycles.cycle_fixtures import PERIOD
from tests.delivery.conftest import DeliverySetup
from tests.delivery.smtp import FakeSMTP
from tests.delivery.test_late_observation_render import (
    accepted_claim,
    acknowledged_publication,
    late_claim,
)

pytestmark = pytest.mark.django_db(transaction=True)


def history(tenant_id: UUID, message: OutboundMessage) -> list[DeliveryObservation]:
    with tenant_context(tenant_id):
        [row] = message_history([message])
    observations = row["timeline"]
    assert isinstance(observations, list)
    typed = [item for item in observations if isinstance(item, DeliveryObservation)]
    assert len(typed) == len(observations)
    return typed


def communication(
    delivery: DeliverySetup, message: OutboundMessage
) -> list[TimelineEntry]:
    with tenant_context(delivery.graph.tenant.pk):
        entries = read_timeline(
            delivery.graph.tenant.pk, delivery.graph.client.pk, PERIOD
        )
    return [entry for entry in entries if entry.subject_ref == str(message.pk)]


def first_claim(
    delivery: DeliverySetup, smtp_server: Callable[[str], FakeSMTP], mode: str
) -> tuple[OutboundMessage, Reply]:
    smtp_server(mode)
    message = delivery.queue()
    claim = claim_batch(delivery.graph.tenant.pk)[0]
    reply = send(claim)
    assert finalize(claim, reply)
    return delivery.reload(message), reply


@pytest.mark.parametrize(
    "late",
    [
        MessageState.CANCELLED,
        MessageState.REJECTED,
        MessageState.TEMPORARY,
        MessageState.UNKNOWN,
    ],
)
def test_a_late_outcome_after_acceptance_is_an_ignored_observation(
    delivery: DeliverySetup,
    smtp_server: Callable[[str], FakeSMTP],
    late: MessageState,
) -> None:
    message, claim = accepted_claim(delivery, smtp_server)
    assert not finalize(claim, Reply(late))
    message = delivery.reload(message)
    assert message.state == MessageState.ACCEPTED

    *earlier, last = history(delivery.graph.tenant.pk, message)

    assert last.effect is ObservationEffect.IGNORED_LATE
    assert last.ignored
    assert last.observed_state == late
    assert last.detail
    assert last.summary != last.observed_label
    assert {item.effect for item in earlier} == {ObservationEffect.APPLIED}
    assert earlier[-1].observed_state == message.state
    assert earlier[-1].summary == earlier[-1].observed_label
    with tenant_context(delivery.graph.tenant.pk):
        receipt = DeliveryAttempt.objects.get(
            message=message, error_code="late_observation"
        )
    assert (receipt.outcome, receipt.occurred_at) == (late, last.when)

    entries = communication(delivery, message)
    assert entries[0].ignored
    assert entries[0].detail == last.detail
    assert entries[0].when == last.when
    assert not any(entry.ignored or entry.detail for entry in entries[1:])
    assert entries[1].summary == earlier[-1].summary


def test_a_genuine_provider_rejection_is_applied_not_ignored(
    delivery: DeliverySetup, smtp_server: Callable[[str], FakeSMTP]
) -> None:
    message, _ = first_claim(delivery, smtp_server, "reject")
    assert message.state == MessageState.REJECTED

    observations = history(delivery.graph.tenant.pk, message)

    assert {item.effect for item in observations} == {ObservationEffect.APPLIED}
    assert observations[-1].observed_state == MessageState.REJECTED
    assert not any(item.ignored or item.detail for item in observations)
    assert not any(entry.ignored for entry in communication(delivery, message))


def test_an_uncertain_outcome_stays_current_when_an_acceptance_arrives_late(
    delivery: DeliverySetup, smtp_server: Callable[[str], FakeSMTP]
) -> None:
    smtp_server("disconnect")
    message = delivery.queue()
    claim = claim_batch(delivery.graph.tenant.pk)[0]
    assert finalize(claim, send(claim))
    assert not finalize(claim, Reply(MessageState.ACCEPTED))
    message = delivery.reload(message)
    assert message.state == MessageState.UNKNOWN

    *earlier, last = history(delivery.graph.tenant.pk, message)

    assert (last.effect, last.observed_state) == (
        ObservationEffect.IGNORED_LATE,
        MessageState.ACCEPTED,
    )
    assert earlier[-1].effect is ObservationEffect.APPLIED
    assert earlier[-1].observed_state == MessageState.UNKNOWN
    entries = communication(delivery, message)
    assert entries[0].ignored
    assert not entries[1].ignored


def test_a_superseded_fence_reply_is_typed_ignored_stale(
    delivery: DeliverySetup, smtp_server: Callable[[str], FakeSMTP]
) -> None:
    smtp_server("accept")
    message = delivery.queue()
    claim = claim_batch(delivery.graph.tenant.pk)[0]
    stale = replace(claim, fence=claim.fence - 1)
    assert not finalize(stale, Reply(MessageState.REJECTED))
    assert finalize(claim, send(claim))
    message = delivery.reload(message)

    observations = history(delivery.graph.tenant.pk, message)

    assert [
        (item.effect, item.observed_state) for item in observations if item.ignored
    ] == [(ObservationEffect.IGNORED_STALE, MessageState.REJECTED)]
    assert observations[-1].effect is ObservationEffect.APPLIED
    assert observations[-1].observed_state == message.state == MessageState.ACCEPTED


def test_an_explicit_client_receipt_survives_a_late_cancellation_untyped(
    settings: SettingsWrapper, tmp_path: Path
) -> None:
    setup, message = acknowledged_publication(settings, tmp_path, "receipt-typed")
    tenant_id = setup.graph.tenant.pk
    assert not finalize(late_claim(tenant_id, message), Reply(MessageState.CANCELLED))
    with tenant_context(tenant_id):
        message.refresh_from_db()
        [row] = message_history([message])
        publication = row["publication"]
        assert isinstance(publication, ClientPublication)
        receipts = list(publication.acknowledgements.all())
        attempts = DeliveryAttempt.objects.filter(message=message).count()

    assert message.state == MessageState.ACCEPTED
    assert [receipt.user_id for receipt in receipts] == [setup.portal_user.pk]
    *earlier, last = history(tenant_id, message)
    assert len(earlier) + 1 == attempts
    assert (last.effect, last.observed_state) == (
        ObservationEffect.IGNORED_LATE,
        MessageState.CANCELLED,
    )
    assert earlier[-1].effect is ObservationEffect.APPLIED
    assert earlier[-1].observed_state == MessageState.ACCEPTED
