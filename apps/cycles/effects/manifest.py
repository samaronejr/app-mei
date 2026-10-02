"""RFC 8785 canonical bytes for the restricted S7.4 manifest domain.

This is not a general IEEE-754 JCS number serializer: every float is rejected,
and integers must be in [-9007199254740991, 9007199254740991]. Decimal amounts
enter JSON as explicitly formatted strings. No Unicode/string normalization or
array sorting is performed by the primitive. EffectManifestV1 normalizes only
the closed manifest's identifiers and set-valued collections.

Source: https://www.rfc-editor.org/rfc/rfc8785 (3.1-3.2.4, Appendices B, D, E).
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import MAX_EMAX, MIN_EMIN, Context, Decimal, InvalidOperation
from enum import StrEnum
from typing import Final, Literal, Never, TypedDict, assert_never
from uuid import UUID

type JSONValue = (
    bool | int | float | str | list[JSONValue] | dict[str, JSONValue] | None
)

_MAX_SAFE_INTEGER: Final = 9007199254740991


def canonicalize(value: JSONValue) -> bytes:
    """Serialize a JSON value to canonical UTF-8 bytes, without a BOM.

    Floats, unsafe integers and surrogate code points raise ValueError.
    JSONValue includes float so dynamically parsed JSON can be checked here;
    accepting the Python input type does not mean it is in the supported profile.
    Raw JSON must use canonicalize_json: a dict has already lost duplicate keys.
    """
    match value:
        case str() | bool() | None:
            return json.dumps(value, ensure_ascii=False).encode("utf-8")
        case int():
            if not -_MAX_SAFE_INTEGER <= value <= _MAX_SAFE_INTEGER:
                message = "Integer outside the interoperable safe domain"
                raise ValueError(message)
            return str(value).encode("ascii")
        case float():
            return _reject_number(str(value))
        case list():
            return b"[" + b",".join(canonicalize(item) for item in value) + b"]"
        case dict():
            # Big-endian bytes compare exactly like unsigned UTF-16 code units.
            keys = sorted(value, key=lambda name: name.encode("utf-16be"))
            return (
                b"{"
                + b",".join(
                    canonicalize(name) + b":" + canonicalize(value[name])
                    for name in keys
                )
                + b"}"
            )
    assert_never(value)


def canonicalize_json(source: str | bytes) -> bytes:
    """Parse JSON without losing duplicates, then emit canonical bytes.

    Byte input must be UTF-8 without a BOM. Malformed JSON, duplicate decoded
    keys, floats/nonfinite numbers and values outside the profile raise ValueError
    (including JSONDecodeError and UnicodeError). UTF-16/32 auto-detection is
    deliberately disabled by decoding bytes before calling the JSON parser.
    """
    if isinstance(source, bytes):
        source = source.decode("utf-8")
    value: JSONValue = json.loads(
        source,
        object_pairs_hook=_unique_object,
        parse_float=_reject_number,
        parse_constant=_reject_number,
    )
    return canonicalize(value)


def _unique_object(pairs: list[tuple[str, JSONValue]]) -> dict[str, JSONValue]:
    result: dict[str, JSONValue] = {}
    for name, value in pairs:
        if name in result:
            message = f"Duplicate JSON key: {name!r}"
            raise ValueError(message)
        result[name] = value
    return result


def _reject_number(token: str) -> Never:
    message = f"Floating-point and nonfinite JSON numbers are unsupported: {token}"
    raise ValueError(message)


def decimal_string(value: Decimal, *, scale: int, rounding: str) -> str:
    """Quantize a finite Decimal to an explicit scale, then format without exponent.

    The caller chooses the rounding rule; ambient Decimal context is never used
    or modified. Signed zero is preserved. Floats are not converted to Decimal.
    Nonfinite values and negative scales raise ValueError; invalid rounding modes
    and Decimal implementation limits retain their standard-library errors.
    """
    if not value.is_finite() or scale < 0:
        message = "A finite Decimal and nonnegative scale are required"
        raise ValueError(message)
    quantum = Decimal((0, (1,), -scale))
    # Include enough coefficient digits for the scale and a possible carry.
    context = Context(
        prec=max(1, len(value.as_tuple().digits), value.adjusted() + scale + 2),
        rounding=rounding,
        Emin=MIN_EMIN,
        Emax=MAX_EMAX,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[InvalidOperation],
    )
    return format(value.quantize(quantum, context=context), "f")


class ActionKind(StrEnum):
    """The four consequential effects in S7.4; execution belongs to T16."""

    CLIENT_PUBLICATION = "client_publication"
    EMAIL_NOTICE = "email_notice"
    CYCLE_COMPLETION = "cycle_completion"
    REQUEST_WAIVER = "request_waiver"


BOUND_COLLECTIONS: Final[dict[ActionKind, frozenset[str]]] = {
    ActionKind.CLIENT_PUBLICATION: frozenset(
        {"recipients", "money", "documents", "evidence", "observations"}
    ),
    ActionKind.EMAIL_NOTICE: frozenset(
        {"recipients", "money", "documents", "observations"}
    ),
    ActionKind.CYCLE_COMPLETION: frozenset(
        {"money", "documents", "evidence", "observations"}
    ),
    ActionKind.REQUEST_WAIVER: frozenset({"evidence"}),
}
MESSAGE_ACTIONS: Final = frozenset(
    {ActionKind.CLIENT_PUBLICATION, ActionKind.EMAIL_NOTICE}
)
_MESSAGE_FIELDS: Final = frozenset(
    {"body", "subject", "sender", "headers", "mime_sha256", "mime_base64"}
)
_SHA256_LENGTH: Final = 64


class Destination(TypedDict):
    """The provider account and operation the frozen effect targets."""

    provider: str
    account_id: str
    operation: str


class Recipient(TypedDict):
    """A selected contact's exact delivery address and channel."""

    role: str
    contact_id: str
    channel: str
    address: str


@dataclass(frozen=True, slots=True)
class MonetaryEffect:
    """An amount with the explicit scale and rounding rule it was rendered with."""

    name: str
    amount: Decimal
    currency: str
    scale: int
    rounding_rule: str


class DocumentEffect(TypedDict):
    """A document and its scan/link revision, including the frozen review result."""

    document_id: str
    sha256: str
    size: int
    intake_state: str
    scan_id: str
    link_id: str
    link_revision: int
    review_state: str


class EvidenceEffect(TypedDict):
    """The evidence request revision and latest decision included in the effect."""

    request_id: str
    revision: int
    decision_id: str | None


class ObservationEffect(TypedDict):
    """An immutable source assertion, with its explicitly derived validity."""

    id: str
    source_kind: str
    status: str
    observed_at: datetime
    valid_until: datetime | None


class PolicySnapshot(TypedDict):
    """The policy identity, revision and canonical content hash."""

    id: str
    version: int
    hash: str


class GrantSnapshot(TypedDict):
    """An actual authority row, never a role name masquerading as a grant."""

    id: str
    version: int
    expires_at: datetime | None


class AuthorizationSnapshot(TypedDict):
    """The initiator's membership and the live grants it relied on."""

    membership_id: str
    membership_version: int
    grants: tuple[GrantSnapshot, ...]


class ApprovalRule(TypedDict):
    """The human authority and quorum needed for this exact effect."""

    required_capability: str
    quorum: int
    separation_of_duties: bool


def utc_timestamp(value: datetime) -> str:
    """Render an aware timestamp as UTC with six fractional digits and Z."""
    if value.tzinfo is None or value.utcoffset() is None:
        message = "Manifest timestamps require a timezone"
        raise ValueError(message)
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def normalize_email(address: str) -> str:
    """Normalize only the IDNA domain; the local part remains byte-sensitive."""
    local, separator, domain = address.rpartition("@")
    if not separator or not local or not domain:
        message = "A recipient requires an email address"
        raise ValueError(message)
    return local + "@" + domain.encode("idna").decode("ascii").lower()


def _uuid(value: str) -> str:
    return str(UUID(value))


def _account_id(value: str) -> str:
    """Normalize UUID accounts without changing an opaque provider identifier."""
    try:
        return _uuid(value)
    except ValueError:
        return value


def _hash(value: str) -> str:
    normalized = value.lower()
    if len(normalized) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        message = "A manifest hash must be a SHA-256 hexadecimal digest"
        raise ValueError(message)
    return normalized


@dataclass(frozen=True, slots=True, kw_only=True)
class EffectManifestV1:
    """A closed value assembled from locked facts, not a loader or an executor.

    Identity and time bounds are supplied by the persisted proposal. No UUID or
    timestamp is generated here. Sets are sorted on normalized canonical member
    bytes; arrays inside the fully rendered payload retain their semantic order.
    Collections irrelevant to an action are empty per S7.4's '-' matrix cells.
    """

    schema: Literal["axorim.effect.v1"] = field(default="axorim.effect.v1", init=False)
    tenant_id: str
    client_id: str
    engagement_id: str
    cycle_id: str
    cycle_revision: int
    amendment: int
    period_start: date
    period_end: date
    timezone: Literal["America/Sao_Paulo"] = field(
        default="America/Sao_Paulo", init=False
    )
    playbook_version_id: str
    playbook_hash: str
    proposal_id: str
    proposal_revision: int
    business_intent_key: str
    action_kind: ActionKind
    adapter_version: str
    confirmation_contract_version: str
    initiator_id: str
    destination: Destination
    payload: dict[str, JSONValue]
    recipients: tuple[Recipient, ...]
    money: tuple[MonetaryEffect, ...]
    documents: tuple[DocumentEffect, ...]
    evidence: tuple[EvidenceEffect, ...]
    observations: tuple[ObservationEffect, ...]
    tax_profile_versions: tuple[str, ...]
    policy: PolicySnapshot
    authorization: AuthorizationSnapshot
    approval_rule: ApprovalRule
    not_before: datetime | None
    expires_at: datetime

    def as_data(self) -> dict[str, JSONValue]:
        """Return the normalized closed JSON object without mutating any input."""
        grants: list[JSONValue] = [
            {
                "id": _uuid(grant["id"]),
                "version": grant["version"],
                "expires_at": (
                    utc_timestamp(grant["expires_at"])
                    if grant["expires_at"] is not None
                    else None
                ),
            }
            for grant in self.authorization["grants"]
        ]
        profiles: list[JSONValue] = [
            _uuid(value) for value in self.tax_profile_versions
        ]
        collections: dict[str, list[JSONValue]] = {
            "recipients": [
                {
                    "role": recipient["role"],
                    "contact_id": _uuid(recipient["contact_id"]),
                    "channel": recipient["channel"],
                    "address": (
                        normalize_email(recipient["address"])
                        if recipient["channel"] == "email"
                        else recipient["address"]
                    ),
                }
                for recipient in self.recipients
            ],
            "money": [
                {
                    "name": amount.name,
                    "decimal": decimal_string(
                        amount.amount, scale=amount.scale, rounding=amount.rounding_rule
                    ),
                    "currency": amount.currency.upper(),
                    "rounding_rule": amount.rounding_rule,
                }
                for amount in self.money
            ],
            "documents": [
                {
                    "document_id": _uuid(document["document_id"]),
                    "sha256": _hash(document["sha256"]),
                    "size": document["size"],
                    "intake_state": document["intake_state"],
                    "scan_id": document["scan_id"].lower(),
                    "link_id": _uuid(document["link_id"]),
                    "link_revision": document["link_revision"],
                    "review_state": document["review_state"],
                }
                for document in self.documents
            ],
            "evidence": [
                {
                    "request_id": _uuid(item["request_id"]),
                    "revision": item["revision"],
                    "decision_id": (
                        _uuid(item["decision_id"])
                        if item["decision_id"] is not None
                        else None
                    ),
                }
                for item in self.evidence
            ],
            "observations": [
                {
                    "id": _uuid(observation["id"]),
                    "source_kind": observation["source_kind"],
                    "status": observation["status"],
                    "observed_at": utc_timestamp(observation["observed_at"]),
                    "valid_until": (
                        utc_timestamp(observation["valid_until"])
                        if observation["valid_until"] is not None
                        else None
                    ),
                }
                for observation in self.observations
            ],
        }
        data: dict[str, JSONValue] = {
            "schema": self.schema,
            "tenant_id": _uuid(self.tenant_id),
            "client_id": _uuid(self.client_id),
            "engagement_id": _uuid(self.engagement_id),
            "cycle_id": _uuid(self.cycle_id),
            "cycle_revision": self.cycle_revision,
            "amendment": self.amendment,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "timezone": self.timezone,
            "playbook_version_id": _uuid(self.playbook_version_id),
            "playbook_hash": _hash(self.playbook_hash),
            "proposal_id": _uuid(self.proposal_id),
            "proposal_revision": self.proposal_revision,
            "business_intent_key": _hash(self.business_intent_key),
            "action_kind": self.action_kind,
            "adapter_version": self.adapter_version,
            "confirmation_contract_version": self.confirmation_contract_version,
            "initiator_id": _uuid(self.initiator_id),
            "destination": {
                "provider": self.destination["provider"],
                "account_id": _account_id(self.destination["account_id"]),
                "operation": self.destination["operation"],
            },
            "payload": {
                key: value
                for key, value in self.payload.items()
                if self.action_kind in MESSAGE_ACTIONS or key not in _MESSAGE_FIELDS
            },
            "tax_profile_versions": sorted(profiles, key=canonicalize),
            "policy": {
                "id": _uuid(self.policy["id"]),
                "version": self.policy["version"],
                "hash": _hash(self.policy["hash"]),
            },
            "authorization": {
                "membership_id": _uuid(self.authorization["membership_id"]),
                "membership_version": self.authorization["membership_version"],
                "grants": sorted(grants, key=canonicalize),
            },
            "approval_rule": {
                "required_capability": self.approval_rule["required_capability"],
                "quorum": self.approval_rule["quorum"],
                "separation_of_duties": self.approval_rule["separation_of_duties"],
            },
            "not_before": utc_timestamp(self.not_before) if self.not_before else None,
            "expires_at": utc_timestamp(self.expires_at),
        }
        data.update(
            {
                name: sorted(items, key=canonicalize)
                if name in BOUND_COLLECTIONS[self.action_kind]
                else []
                for name, items in collections.items()
            }
        )
        return data

    def canonical_bytes(self) -> bytes:
        """Return the exact JCS bytes the proposal must persist."""
        return canonicalize(self.as_data())

    @property
    def digest(self) -> str:
        """Return the lowercase SHA-256 of the persisted representation."""
        return hashlib.sha256(self.canonical_bytes()).hexdigest()
