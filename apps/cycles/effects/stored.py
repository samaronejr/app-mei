"""The one reader of a stored proposal's input blob (AXRM-022, deliberate, kept).

The database binds a proposal's row kind to its frozen manifest, never to its input
JSON. Every decode site therefore reads through here: the stored COLUMN decides, and a
blob that contradicts it is an integrity refusal, never an invalidation of effects.
"""

from django.core.exceptions import ValidationError

from apps.cycles.effects.inputs import ProposalInput
from apps.cycles.effects.manifest import ActionKind, kind_unavailable
from apps.cycles.errors import ProposalIntegrityError
from apps.cycles.models import ActionProposal

INTEGRITY_MESSAGE = "Os dados desta proposta não correspondem ao efeito registrado."
KNOWN_KINDS = frozenset(str(kind) for kind in ActionKind)


def refuse_unavailable(kind: str) -> None:
    """AXRM-022-SCAFFOLD: no fiscal effect is proposable or decidable yet.

    22-06 keeps this refusal keyed on the ROW kind; removed in 22-14. By value: a bare
    string counts.
    """
    if kind_unavailable(kind):
        msg = "A ciência da operação ainda não pode ser proposta nem decidida."
        raise ValidationError(msg, code="fiscal_manifestation_unavailable")


def known_kind(proposal: ActionProposal) -> ActionKind:
    """Return the stored column as a kind; an unknown column is an integrity error."""
    if proposal.action_kind not in KNOWN_KINDS:
        raise ProposalIntegrityError(INTEGRITY_MESSAGE)
    return ActionKind(proposal.action_kind)


def stored_kind(proposal: ActionProposal) -> ActionKind:
    """Return the row's own kind, before any blob is read.

    A kind with no effect yet is refused; an unknown one is an integrity error.
    """
    refuse_unavailable(proposal.action_kind)
    return known_kind(proposal)


def stored_input(proposal: ActionProposal) -> ProposalInput:
    """Decode the blob only once its discriminator names the row's own kind."""
    kind = stored_kind(proposal)
    data = proposal.input_spec
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("action_kind"), str)
        or data["action_kind"] not in KNOWN_KINDS
        or data["action_kind"] != kind
    ):
        raise ProposalIntegrityError(INTEGRITY_MESSAGE)
    try:
        spec = ProposalInput.from_data(data)
    except (ValidationError, ValueError) as error:
        # The expected decode failures: shape, non-text field, malformed identifier.
        raise ProposalIntegrityError(INTEGRITY_MESSAGE) from error
    return spec
