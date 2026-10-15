"""Closed completion input rejects malformed or action-inappropriate selections."""

from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from django.core.exceptions import ValidationError

from apps.cycles.effects.inputs import ProposalInput
from apps.cycles.effects.manifest import ActionKind

if TYPE_CHECKING:
    from apps.cycles.effects.manifest import JSONValue

GENERATION = "00000000-0000-4000-8000-000000000018"
SUBJECT = "00000000-0000-4000-8000-000000000019"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("reopen", None),
        ("reopen", {}),
        ("reopen", {"generation_id": GENERATION, "affected_scope": []}),
        ("reopen", {"generation_id": GENERATION, "affected_scope": [SUBJECT, SUBJECT]}),
        ("completion_reasons", ["not a mapping"]),
        ("completion_reasons", {"": "reason"}),
        ("completion_reasons", {"tasks:subject:1": ""}),
        ("completion_reasons", {"tasks:subject:1": "x" * 2001}),
        ("unknown_field", 1),
        ("occurrence", True),
        ("occurrence", 0),
        ("action_kind", "unknown"),
    ],
)
def test_malformed_completion_input_fails_closed(key: str, value: "JSONValue") -> None:
    data = ProposalInput(
        action_kind=ActionKind.CYCLE_COMPLETION, step_key="completion", reason="Reason"
    ).as_data()
    data[key] = value
    with pytest.raises((ValidationError, ValueError)):
        ProposalInput.from_data(data)


@pytest.mark.parametrize("key", ["completion_reasons", "completion_state"])
def test_completion_fields_are_not_accepted_for_a_valid_email(key: str) -> None:
    data = ProposalInput(
        action_kind=ActionKind.EMAIL_NOTICE,
        step_key="email",
        recipient_ids=(UUID(SUBJECT),),
        subject="Subject",
        body="Body",
    ).as_data()
    assert ProposalInput.from_data(data).action_kind is ActionKind.EMAIL_NOTICE
    data[key] = {} if key == "completion_reasons" else "concluido"
    with pytest.raises(ValidationError):
        ProposalInput.from_data(data)


def test_reopen_parser_preserves_exact_generation_and_scope() -> None:
    data = ProposalInput(
        action_kind=ActionKind.CYCLE_COMPLETION, step_key="reopen", reason="Amendment"
    ).as_data()
    data["reopen"] = {"generation_id": GENERATION, "affected_scope": [SUBJECT]}
    parsed = ProposalInput.from_data(data)
    assert parsed.reopen
    assert parsed.generation_id == UUID(GENERATION)
    assert parsed.affected_scope == (UUID(SUBJECT),)
