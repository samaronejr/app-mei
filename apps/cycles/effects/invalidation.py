"""S7.4's complete matrix over frozen bytes and explicit live authority results.

This module does not grant authority or execute effects. Its caller must rebuild
live facts and supply the authorization result while holding the required locks.
Warnings and a future execution window are distinct from invalidation.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from apps.cycles.effects.manifest import (
    MESSAGE_ACTIONS,
    ActionKind,
    EffectManifestV1,
    JSONValue,
    canonicalize_json,
    utc_timestamp,
)


class Change(StrEnum):
    """The ten rows in the exact-effect invalidation matrix."""

    RECIPIENTS = "recipients_changed"
    DOCUMENTS = "documents_changed"
    EVIDENCE = "evidence_changed"
    AMOUNT = "amount_changed"
    POLICY = "policy_changed"
    APPROVER = "approver_role_revoked"
    ACCESS_EXPIRED = "grant_or_support_expired"
    SOURCE_STALE = "source_stale"
    EXPIRY = "proposal_expired"
    BODY = "body_text_changed"


class Impact(StrEnum):
    """Ignore, warn, invalidate, or wait for an unchanged future time bound."""

    IGNORE = "ignore"
    WARN = "warn"
    INVALIDATE = "invalidate"
    WAIT = "wait"


@dataclass(frozen=True, slots=True)
class InvalidationReason:
    """A stable machine code and its effect on approval and execution."""

    code: str
    impact: Impact

    @property
    def invalidates(self) -> bool:
        """Whether the current approval must be invalidated."""
        return self.impact is Impact.INVALIDATE

    @property
    def blocks_execution(self) -> bool:
        """Warnings alone do not block an email; a future window still does."""
        return self.impact in {Impact.INVALIDATE, Impact.WAIT}


@dataclass(frozen=True, slots=True)
class RevalidationContext:
    """Results of the caller's live, locked checks; none default to an allow."""

    now: datetime
    approver_authorized: bool
    grant_access_valid: bool


_I: Final = Impact.INVALIDATE
_N: Final = Impact.IGNORE
_W: Final = Impact.WARN
# Columns: client_publication, email_notice, cycle_completion, request_waiver.
# The email document cell applies to documents actually linked into that effect.
_MATRIX: Final[dict[Change, tuple[Impact, Impact, Impact, Impact]]] = {
    Change.RECIPIENTS: (_I, _I, _N, _N),
    Change.DOCUMENTS: (_I, _I, _I, _N),
    Change.EVIDENCE: (_I, _N, _I, _I),
    Change.AMOUNT: (_I, _I, _I, _N),
    Change.POLICY: (_I, _I, _I, _I),
    Change.APPROVER: (_I, _I, _I, _I),
    Change.ACCESS_EXPIRED: (_I, _I, _I, _I),
    Change.SOURCE_STALE: (_I, _W, _I, _N),
    Change.EXPIRY: (_I, _I, _I, _I),
    Change.BODY: (_I, _I, _N, _N),
}
_COLUMNS: Final = {kind: index for index, kind in enumerate(ActionKind)}
_FIELDS: Final = frozenset(field.name for field in fields(EffectManifestV1))
_COLLECTION_CHANGES: Final = {
    Change.RECIPIENTS: "recipients",
    Change.DOCUMENTS: "documents",
    Change.EVIDENCE: "evidence",
    Change.AMOUNT: "money",
}
_CONTENT_FIELDS: Final = frozenset(
    {
        *_COLLECTION_CHANGES.values(),
        "policy",
        "approval_rule",
        "payload",
        "observations",
        "authorization",
    }
)


def assess_changes(
    kind: ActionKind, changes: tuple[Change, ...]
) -> tuple[InvalidationReason, ...]:
    """Apply every matrix cell, returning stable, deduplicated reason order."""
    column = _COLUMNS[kind]
    return tuple(
        InvalidationReason(change.value, _MATRIX[change][column])
        for change in Change
        if change in changes and _MATRIX[change][column] is not Impact.IGNORE
    )


def _object(value: JSONValue) -> dict[str, JSONValue]:
    if not isinstance(value, dict):
        message = "A manifest object is required"
        raise TypeError(message)
    return value


def _rows(value: JSONValue) -> list[dict[str, JSONValue]]:
    if not isinstance(value, list):
        message = "A manifest collection is required"
        raise TypeError(message)
    return [_object(item) for item in value]


def _text(value: JSONValue) -> str:
    if not isinstance(value, str):
        message = "A manifest string is required"
        raise TypeError(message)
    return value


def _time(value: JSONValue) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(_text(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        message = "Manifest timestamps require a timezone"
        raise ValueError(message)
    return parsed.astimezone(UTC)


def _read(raw: bytes) -> dict[str, JSONValue]:
    if canonicalize_json(raw) != raw:
        message = "Persisted manifests must already be canonical"
        raise ValueError(message)
    value: JSONValue = json.loads(raw)
    data = _object(value)
    if set(data) != _FIELDS or data["schema"] != "axorim.effect.v1":
        message = "The closed EffectManifestV1 schema is required"
        raise ValueError(message)
    return data


def _expired_grant(data: Mapping[str, JSONValue], now: datetime) -> bool:
    grants = _rows(_object(data["authorization"])["grants"])
    return any(
        expiry is not None and expiry <= now
        for expiry in (_time(grant["expires_at"]) for grant in grants)
    )


def _stale_source(data: Mapping[str, JSONValue], now: datetime) -> bool:
    return any(
        expiry is not None and expiry <= now
        for expiry in (
            _time(source["valid_until"]) for source in _rows(data["observations"])
        )
    )


def _changes(
    before: Mapping[str, JSONValue],
    after: Mapping[str, JSONValue],
    context: RevalidationContext,
) -> tuple[Change, ...]:
    changes = [
        change
        for change, field_name in _COLLECTION_CHANGES.items()
        if before[field_name] != after[field_name]
    ]
    if (
        before["policy"] != after["policy"]
        or before["approval_rule"] != after["approval_rule"]
    ):
        changes.append(Change.POLICY)
    if not context.approver_authorized:
        changes.append(Change.APPROVER)
    if not context.grant_access_valid or _expired_grant(before, context.now):
        changes.append(Change.ACCESS_EXPIRED)
    if _stale_source(after, context.now):
        changes.append(Change.SOURCE_STALE)
    expires_at = _time(after["expires_at"])
    if expires_at is None:
        message = "A proposal must expire"
        raise ValueError(message)
    if expires_at <= context.now:
        changes.append(Change.EXPIRY)
    if before["payload"] != after["payload"]:
        changes.append(Change.BODY)
    return tuple(changes)


def compare_manifests(
    previous: bytes,
    current: bytes,
    *,
    context: RevalidationContext,
) -> tuple[InvalidationReason, ...]:
    """Compare stored approval bytes to a live rebuild, failing closed on damage.

    An action's irrelevant collections are empty in EffectManifestV1. Source
    freshness can expire without a byte changing, which is why time and live
    authorization are explicit inputs rather than synthetic manifest fields.
    """
    try:
        before, after = _read(previous), _read(current)
        kind = ActionKind(_text(before["action_kind"]))
        _ = ActionKind(_text(after["action_kind"]))
        _ = utc_timestamp(context.now)
        reasons = list(assess_changes(kind, _changes(before, after, context)))
        extra_changes = {
            "manifest_identity_changed": any(
                before[name] != after[name] for name in _FIELDS - _CONTENT_FIELDS
            ),
            "authorization_changed": before["authorization"] != after["authorization"],
            "source_changed": before["observations"] != after["observations"],
            "payload_changed": (
                kind not in MESSAGE_ACTIONS and before["payload"] != after["payload"]
            ),
        }
        reasons.extend(
            InvalidationReason(code, Impact.INVALIDATE)
            for code, changed in extra_changes.items()
            if changed
        )
        not_before = _time(after["not_before"])
        if not_before is not None and context.now < not_before:
            reasons.append(InvalidationReason("not_before", Impact.WAIT))
    except (ValueError, KeyError, TypeError):
        return (InvalidationReason("manifest_invalid", Impact.INVALIDATE),)
    return tuple(reasons)
