"""AXR1-043: the typed view model behind the workspace's request and message rows.

This module imports the new read models, so at b9bba19 it fails at collection; the
behavioural red witness is test_workspace_sections_render.py. Assertions read typed
fields, machine values and query counts only.
"""

from pathlib import Path

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from pytest_django.fixtures import SettingsWrapper

from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.cycles.effects.delivery import APPROVED_KINDS
from apps.cycles.evidence import RequestKind
from apps.cycles.models import RequestState
from apps.cycles.publications import PublicationState
from apps.cycles.services.requests import RequestFields, create_request
from apps.cycles.workspace_sections import PeriodRequest, period_requests
from apps.delivery.models import MessageState
from apps.delivery.policy import KINDS
from apps.delivery.timeline import (
    MESSAGE_KIND_LABELS,
    REDACTED,
    UNKNOWN_KIND_LABEL,
    ObservationEffect,
    PeriodCommunication,
    period_communication,
)
from tests.cycles.cycle_fixtures import PERIOD
from tests.cycles.test_workspace_sections_render import (
    NEXT_PERIOD,
    PeriodWorld,
    deliver_notice,
    period_world,
)

pytestmark = pytest.mark.django_db(transaction=True)


def test_every_registered_message_kind_has_a_reader_label() -> None:
    assert set(KINDS) | APPROVED_KINDS <= set(MESSAGE_KIND_LABELS)
    assert UNKNOWN_KIND_LABEL not in MESSAGE_KIND_LABELS.values()


def test_context_types_requests_messages_receipts_and_the_empty_period(
    settings: SettingsWrapper, tmp_path: Path
) -> None:
    world = period_world(settings, tmp_path, "t43-typed")
    firm = world.setup.firm()

    response = firm.get(world.url())
    empty = firm.get(world.url(NEXT_PERIOD))

    requests = response.context["period_requests"]
    assert all(isinstance(row, PeriodRequest) for row in requests)
    assert [
        (row.request_id, row.cycle_id, row.kind, row.state, row.client_status)
        for row in requests
    ] == [
        (
            world.request.pk,
            world.setup.cycle.pk,
            RequestKind.DOCUMENTO,
            RequestState.REQUESTED,
            "atrasada",
        )
    ]
    assert requests[0].requested_at == world.request.requested_at
    assert requests[0].obligation_month is None
    communication = response.context["communication"]
    assert isinstance(communication, PeriodCommunication)
    assert not communication.more_messages
    by_id = {row.message.pk: row for row in communication.messages}
    assert set(by_id) == {world.request_notice.pk, world.publication_notice.pk}
    notice = by_id[world.request_notice.pk]
    assert notice.message.state == MessageState.ACCEPTED
    assert notice.kind_label == MESSAGE_KIND_LABELS["request_notice"]
    *earlier, last = notice.observations
    assert (last.effect, last.observed_state) == (
        ObservationEffect.IGNORED_LATE,
        MessageState.CANCELLED,
    )
    assert {item.effect for item in earlier} == {ObservationEffect.APPLIED}
    assert earlier[-1].observed_state == notice.message.state
    [publication] = communication.publications
    assert publication.publication.pk == world.publication.pk
    assert publication.publication.state == PublicationState.PUBLISHED
    assert publication.acknowledged
    assert [receipt.user_id for receipt in publication.acknowledgements] == [
        world.setup.portal_user.pk
    ]
    assert empty.status_code == 200
    assert empty.context["period_requests"] == []
    assert empty.context["communication"] == PeriodCommunication(
        messages=(), more_messages=False, publications=()
    )


def test_a_support_rendering_masks_subjects_and_titles_but_keeps_outcomes(
    settings: SettingsWrapper, tmp_path: Path
) -> None:
    world = period_world(settings, tmp_path, "t43-redact")

    with tenant_context(world.setup.graph.tenant.pk):
        masked = period_communication(world.setup.graph.client.pk, PERIOD, redact=True)

    assert {row.subject for row in masked.messages} == {REDACTED}
    assert {row.title for row in masked.publications} == {REDACTED}
    assert {row.message.state for row in masked.messages} == {MessageState.ACCEPTED}
    assert [row.acknowledged for row in masked.publications] == [True]


def _measure(world: PeriodWorld) -> tuple[int, int, int, int]:
    with tenant_context(world.setup.graph.tenant.pk):
        client = ClientCompany.objects.get(pk=world.setup.graph.client.pk)
        with CaptureQueriesContext(connection) as request_queries:
            requests = period_requests(client, PERIOD)
        with CaptureQueriesContext(connection) as communication_queries:
            communication = period_communication(client.pk, PERIOD)
    return (
        len(requests),
        len(communication.messages),
        len(request_queries),
        len(communication_queries),
    )


def test_each_section_costs_fixed_queries_however_many_rows(
    settings: SettingsWrapper, tmp_path: Path
) -> None:
    world = period_world(settings, tmp_path, "t43-queries")
    before = _measure(world)
    deliver_notice(world, "reject", occurrence=1)
    deliver_notice(world, "disconnect", occurrence=2)
    with tenant_context(world.setup.graph.tenant.pk):
        world.setup.cycle.refresh_from_db()
        create_request(
            world.setup.graph.actor,
            world.setup.cycle.pk,
            RequestFields(title="Extrato bancário do período"),
            expected_version=world.setup.cycle.version,
        )

    after = _measure(world)

    assert before == (1, 2, 1, 4)
    assert after == (2, 4, 1, 4)
