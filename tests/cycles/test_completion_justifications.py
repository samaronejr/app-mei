"""Reasons authorize only the current unmet revisions, never future conditions."""

import json

import pytest
from django.core.exceptions import ValidationError

from apps.core.tenancy import tenant_context
from apps.cycles.completion_checks import completion_items
from apps.cycles.completion_records import CompletionGeneration, CompletionJustification
from apps.cycles.effects.executors import execute
from apps.cycles.evidence_index import capture_index
from apps.cycles.models import ActionExecution, ActionProposal, CycleEvent, CycleState
from apps.cycles.services.proposed_transitions import propose_completion
from apps.cycles.services.requests import RequestFields, create_request, update_request
from tests.cycles.completion_fixtures import approved_completion, completion_setup

pytestmark = pytest.mark.django_db


def test_each_current_pending_item_is_frozen_in_private_justification() -> None:
    setup = completion_setup("completion-pending-reasons")
    with tenant_context(setup.graph.tenant.pk):
        request = create_request(
            setup.graph.actor,
            setup.cycle.pk,
            RequestFields("Arquivo pendente"),
            expected_version=setup.cycle.version,
        )
        setup.cycle.refresh_from_db()
        capture_index(
            setup.graph.actor, setup.cycle.pk, expected_version=setup.cycle.version
        )
        reasons = {
            item.key: "PRIVATE-JUSTIFICATION-CANARY"
            for item in completion_items(setup.cycle)
        }
        assert set(reasons) == {f"requests:{request.pk}:{request.version}"}
        proposal = approved_completion(
            setup, state=CycleState.WITH_PENDING, reasons=reasons, capture=False
        )
        assert execute(
            setup.graph.actor, proposal.pk, expected_version=proposal.version
        ).executed
        generation = CompletionGeneration.objects.get(cycle=setup.cycle)
        assert (
            CompletionJustification.objects.get(generation=generation).reasons
            == reasons
        )
        setup.cycle.refresh_from_db()
        assert setup.cycle.state == CycleState.WITH_PENDING
        events = list(CycleEvent.objects.values_list("payload", flat=True))
        assert "PRIVATE-JUSTIFICATION-CANARY" not in json.dumps(events)
        assert "PRIVATE-JUSTIFICATION-CANARY" not in json.dumps(generation.snapshot)


def test_unknown_reason_key_is_rejected_before_any_proposal_or_event() -> None:
    setup = completion_setup("completion-reason-key")
    with tenant_context(setup.graph.tenant.pk):
        before = (ActionProposal.objects.count(), CycleEvent.objects.count())
        with pytest.raises(ValidationError):
            propose_completion(
                setup.graph.staff,
                setup.cycle.pk,
                to_state=CycleState.WITH_PENDING,
                reason={"requests:old:1": "Motivo"},
                expected_version=setup.cycle.version,
            )
        assert (ActionProposal.objects.count(), CycleEvent.objects.count()) == before


def test_old_pending_reason_does_not_cover_a_new_request_revision() -> None:
    setup = completion_setup("completion-stale-reason")
    with tenant_context(setup.graph.tenant.pk):
        request = create_request(
            setup.graph.actor,
            setup.cycle.pk,
            RequestFields("Arquivo pendente"),
            expected_version=setup.cycle.version,
        )
        setup.cycle.refresh_from_db()
        capture_index(
            setup.graph.actor, setup.cycle.pk, expected_version=setup.cycle.version
        )
        reasons = {
            item.key: "Motivo anterior" for item in completion_items(setup.cycle)
        }
        proposal = approved_completion(
            setup, state=CycleState.WITH_PENDING, reasons=reasons, capture=False
        )
        update_request(
            setup.graph.actor,
            request.pk,
            RequestFields("Documento corrigido"),
            expected_version=request.version,
        )
        setup.cycle.refresh_from_db()
        capture_index(
            setup.graph.actor, setup.cycle.pk, expected_version=setup.cycle.version
        )
        result = execute(
            setup.graph.actor, proposal.pk, expected_version=proposal.version
        )
        assert not result.executed
        assert not ActionExecution.objects.filter(proposal=proposal).exists()
        assert not CompletionGeneration.objects.filter(cycle=setup.cycle).exists()
