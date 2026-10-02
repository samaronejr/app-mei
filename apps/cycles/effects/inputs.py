"""Explicit selections and already-rendered copy behind a frozen proposal."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from django.core.exceptions import ValidationError

from apps.cycles.effects.manifest import ActionKind, JSONValue

CURRENCY_CODE_LENGTH = 3


def _text(value: JSONValue) -> str:
    if not isinstance(value, str):
        msg = "O texto da proposta é inválido."
        raise ValidationError(msg)
    return value


def _ids(value: JSONValue) -> tuple[UUID, ...]:
    if not isinstance(value, list):
        msg = "Selecione os registros da proposta."
        raise ValidationError(msg)
    return tuple(UUID(_text(item)) for item in value)


@dataclass(frozen=True, slots=True)
class MoneyBinding:
    """A real extracted amount, not a caller-supplied assertion of its value."""

    link_id: UUID
    field_name: str
    currency: str = "BRL"
    scale: int = 2
    rounding_rule: str = "ROUND_HALF_UP"

    def as_data(self) -> dict[str, JSONValue]:
        """Persist the selected field and explicit numeric contract."""
        return {
            "link_id": str(self.link_id),
            "field_name": self.field_name,
            "currency": self.currency,
            "scale": self.scale,
            "rounding_rule": self.rounding_rule,
        }

    @classmethod
    def from_data(cls, data: JSONValue) -> "MoneyBinding":
        """Read the closed persisted selection, rejecting malformed input."""
        if not isinstance(data, dict) or set(data) != {
            "link_id",
            "field_name",
            "currency",
            "scale",
            "rounding_rule",
        }:
            msg = "A referência monetária é inválida."
            raise ValidationError(msg)
        scale = data["scale"]
        if type(scale) is not int:
            msg = "A escala monetária deve ser inteira."
            raise ValidationError(msg)
        return cls(
            UUID(_text(data["link_id"])),
            _text(data["field_name"]),
            _text(data["currency"]),
            scale,
            _text(data["rounding_rule"]),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ProposalInput:
    """Intended sets are explicit; rebuilding never expands to every live row."""

    action_kind: ActionKind
    step_key: str
    occurrence: int = 1
    recipient_ids: tuple[UUID, ...] = ()
    document_link_ids: tuple[UUID, ...] = ()
    evidence_request_ids: tuple[UUID, ...] = ()
    observation_ids: tuple[UUID, ...] = ()
    money: tuple[MoneyBinding, ...] = ()
    subject: str = ""
    body: str = ""
    reason: str = ""

    def validate(self) -> None:
        """Validate at the proposal boundary, before any persistent write."""
        if not self.step_key or self.occurrence < 1:
            msg = "Informe a etapa e uma ocorrência positiva."
            raise ValidationError(msg)
        if "\r" in self.subject or "\n" in self.subject:
            msg = "O assunto deve ocupar uma única linha."
            raise ValidationError(msg)
        selections = (
            self.recipient_ids,
            self.document_link_ids,
            self.evidence_request_ids,
            self.observation_ids,
        )
        if any(len(values) != len(set(values)) for values in selections):
            msg = "Selecione cada registro apenas uma vez."
            raise ValidationError(msg)
        if self.action_kind in {
            ActionKind.EMAIL_NOTICE,
            ActionKind.CLIENT_PUBLICATION,
        } and (
            not self.recipient_ids or not self.subject.strip() or not self.body.strip()
        ):
            msg = "Selecione destinatários e confira o assunto e a mensagem."
            raise ValidationError(msg)
        if self.action_kind is ActionKind.REQUEST_WAIVER and (
            len(self.evidence_request_ids) != 1 or not self.reason.strip()
        ):
            msg = "Selecione uma solicitação e registre o motivo da dispensa."
            raise ValidationError(msg)
        if any(
            item.field_name not in {"valor_total", "valor_servico"}
            or len(item.currency) != CURRENCY_CODE_LENGTH
            or not item.currency.isascii()
            or not item.currency.isalpha()
            for item in self.money
        ):
            msg = "Selecione um valor extraído e sua moeda."
            raise ValidationError(msg)

    def as_data(self) -> dict[str, JSONValue]:
        """Store source ids and rendered copy, never regenerated identities."""
        return {
            "action_kind": self.action_kind,
            "step_key": self.step_key,
            "occurrence": self.occurrence,
            "recipients": [str(value) for value in self.recipient_ids],
            "document_links": [str(value) for value in self.document_link_ids],
            "evidence_requests": [str(value) for value in self.evidence_request_ids],
            "observations": [str(value) for value in self.observation_ids],
            "money": [item.as_data() for item in self.money],
            "subject": self.subject,
            "body": self.body,
            "reason": self.reason,
        }

    @classmethod
    def from_data(cls, data: dict[str, JSONValue]) -> "ProposalInput":
        """Load exactly the persisted input schema, without permissive defaults."""
        if set(data) != {
            "action_kind",
            "step_key",
            "occurrence",
            "recipients",
            "document_links",
            "evidence_requests",
            "observations",
            "money",
            "subject",
            "body",
            "reason",
        }:
            msg = "A seleção persistida da proposta é inválida."
            raise ValidationError(msg)
        occurrence, money = data["occurrence"], data["money"]
        if type(occurrence) is not int or not isinstance(money, list):
            msg = "A seleção persistida da proposta é inválida."
            raise ValidationError(msg)
        result = cls(
            action_kind=ActionKind(_text(data["action_kind"])),
            step_key=_text(data["step_key"]),
            occurrence=occurrence,
            recipient_ids=_ids(data["recipients"]),
            document_link_ids=_ids(data["document_links"]),
            evidence_request_ids=_ids(data["evidence_requests"]),
            observation_ids=_ids(data["observations"]),
            money=tuple(MoneyBinding.from_data(item) for item in money),
            subject=_text(data["subject"]),
            body=_text(data["body"]),
            reason=_text(data["reason"]),
        )
        result.validate()
        return result


@dataclass(frozen=True, slots=True)
class ProposalDraft:
    """Selected effects, validity and an optional explicit previous revision."""

    inputs: ProposalInput
    expires_at: datetime
    not_before: datetime | None = None
    previous_id: UUID | None = None
    previous_version: int | None = None
