"""RFC 8785 canonical bytes for the restricted S7.4 manifest domain.

This is not a general IEEE-754 JCS number serializer: every float is rejected,
and integers must be in [-9007199254740991, 9007199254740991]. Decimal amounts
enter JSON as explicitly formatted strings. No Unicode/string normalization or
array sorting is performed; the later manifest builder owns business semantics.

Source: https://www.rfc-editor.org/rfc/rfc8785 (3.1-3.2.4, Appendices B, D, E).
"""

import json
from decimal import MAX_EMAX, MIN_EMIN, Context, Decimal, InvalidOperation
from typing import Final, Never, assert_never

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
