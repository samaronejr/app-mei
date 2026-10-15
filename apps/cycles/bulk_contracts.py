"""Closed frozen selections: no queryset or callable survives a bulk preview."""

import hashlib
from dataclasses import dataclass
from datetime import date
from typing import Final
from uuid import UUID

from django.core.exceptions import ValidationError

from apps.cycles.effects.manifest import JSONValue, canonicalize

MAX_ITEMS: Final = 1000


@dataclass(frozen=True, slots=True)
class PreviewExpectation:
    """The immutable digest and control revision rendered on the confirmation form."""

    version: int
    digest: str


@dataclass(frozen=True, slots=True)
class Selection:
    """Explicit subject identities and the two operation-specific inputs."""

    subject_ids: tuple[UUID, ...]
    period: date | None = None
    playbook_version_id: UUID | None = None

    def validate(self) -> None:
        """Reject ambiguous identities and non-month competence at the boundary."""
        if len(self.subject_ids) > MAX_ITEMS or len(self.subject_ids) != len(
            set(self.subject_ids)
        ):
            msg = "Selecione até 1.000 registros distintos."
            raise ValidationError(msg)
        if self.period is not None and self.period.day != 1:
            msg = "A competência deve começar no primeiro dia do mês."
            raise ValidationError(msg)


def object_value(value: JSONValue) -> dict[str, JSONValue]:
    """Parse one persisted object, failing closed rather than expanding defaults."""
    if not isinstance(value, dict):
        msg = "A prévia armazenada é inválida. Gere outra prévia."
        raise ValidationError(msg)
    return value


def list_value(value: JSONValue) -> list[JSONValue]:
    """Parse a persisted collection."""
    if not isinstance(value, list):
        msg = "A seleção armazenada é inválida. Gere outra prévia."
        raise ValidationError(msg)
    return value


def text_value(value: JSONValue) -> str:
    """Parse an exact identifier or rendered string."""
    if not isinstance(value, str):
        msg = "A referência armazenada é inválida. Gere outra prévia."
        raise ValidationError(msg)
    return value


def integer_value(value: JSONValue) -> int:
    """Reject booleans and strings masquerading as revision integers."""
    if type(value) is not int:
        msg = "A revisão armazenada é inválida. Gere outra prévia."
        raise ValidationError(msg)
    return value


def digest(value: JSONValue) -> str:
    """Bind the frozen JSON with the same canonical primitive as T15."""
    return hashlib.sha256(canonicalize(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class SnapshotItem:
    """One visible subject and the exact effect inputs shown to the operator."""

    subject_ref: UUID
    client_id: UUID
    label: str
    expected_version: int
    eligible: bool
    reason: str
    effect: dict[str, JSONValue]

    def as_data(self) -> dict[str, JSONValue]:
        """Materialize the immutable item, including independently versioned sources."""
        return {
            "subject_ref": str(self.subject_ref),
            "client_id": str(self.client_id),
            "label": self.label,
            "expected_version": self.expected_version,
            "eligible": self.eligible,
            "reason": self.reason,
            "effect": self.effect,
        }

    @classmethod
    def from_data(cls, value: JSONValue) -> "SnapshotItem":
        """Read the closed schema before confirmation or execution."""
        data = object_value(value)
        if (
            set(data)
            != {
                "subject_ref",
                "client_id",
                "label",
                "expected_version",
                "eligible",
                "reason",
                "effect",
            }
            or type(data["eligible"]) is not bool
        ):
            msg = "O item armazenado é inválido. Gere outra prévia."
            raise ValidationError(msg)
        return cls(
            UUID(text_value(data["subject_ref"])),
            UUID(text_value(data["client_id"])),
            text_value(data["label"]),
            integer_value(data["expected_version"]),
            data["eligible"],
            text_value(data["reason"]),
            object_value(data["effect"]),
        )


def snapshot_items(snapshot: JSONValue) -> tuple[SnapshotItem, ...]:
    """Load the stored identities, never the original selection query."""
    data = object_value(snapshot)
    if data.get("schema") != "axorim.bulk.v1":
        msg = "A versão desta prévia não é suportada."
        raise ValidationError(msg)
    return tuple(SnapshotItem.from_data(row) for row in list_value(data["items"]))
