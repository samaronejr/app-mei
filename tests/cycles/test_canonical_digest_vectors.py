"""T15's restricted JCS profile; no Django, fixtures, or external resources.

Sources: https://www.rfc-editor.org/rfc/rfc8785 (3.2.2-3.2.4, Appendices B-E).
The RFC permits IEEE-754 numbers; this profile deliberately excludes all floats
and integers outside Appendix B note 1's interoperable range. RFC examples with
floats are either projected onto their supported members or tested as refusals.
"""

import hashlib
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, localcontext
from itertools import permutations
from json import JSONDecodeError
from typing import TYPE_CHECKING

import pytest

from apps.cycles.effects.manifest import (
    canonicalize,
    canonicalize_json,
    decimal_string,
)

if TYPE_CHECKING:
    from apps.cycles.effects.manifest import JSONValue


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("null", b"null"),
        ("true", b"true"),
        ("false", b"false"),
        ("0", b"0"),
        ("-0", b"0"),
        ("9007199254740991", b"9007199254740991"),
        ("-9007199254740991", b"-9007199254740991"),
        ('""', b'""'),
        ("[]", b"[]"),
        ("{}", b"{}"),
        (
            ' { "b" : 2, "a" : [true, null, false] } \n',
            b'{"a":[true,null,false],"b":2}',
        ),
    ],
)
def test_supported_json_scalars_and_boundaries(source: str, expected: bytes) -> None:
    assert canonicalize_json(source) == expected
    assert canonicalize_json(source.encode("utf-8")) == expected


def test_rfc_string_and_literals_without_excluded_float_member() -> None:
    source = (
        r'{"string":"\u20ac$\u000F\u000aA'
        r"""'\u0042\u0022\u005c\\\"\/","literals":[null,true,false]}"""
    )
    # The string bytes are taken from RFC 8785 section 3.2.4's hex listing.
    string_bytes = bytes.fromhex(
        "22 e2 82 ac 24 5c 75 30 30 30 66 5c 6e 41 27 42 5c 22 5c 5c 5c 5c 5c 22 2f 22"
    )

    assert canonicalize_json(source) == b'{"literals":[null,true,false],"string":' + (
        string_bytes + b"}"
    )


def test_rfc_key_order_uses_utf16_not_code_points() -> None:
    source = (
        r'{"\u20ac":"Euro Sign","\r":"Carriage Return",'
        r'"\ufb33":"Hebrew Letter Dalet With Dagesh","1":"One",'
        r'"\ud83d\ude00":"Emoji: Grinning Face","\u0080":"Control",'
        r'"\u00f6":"Latin Small Letter O With Diaeresis"}'
    )
    expected = (
        '{"\\r":"Carriage Return","1":"One","\u0080":"Control",'
        '"\u00f6":"Latin Small Letter O With Diaeresis","\u20ac":"Euro Sign",'
        '"\U0001f600":"Emoji: Grinning Face",'
        '"\ufb33":"Hebrew Letter Dalet With Dagesh"}'
    ).encode("utf-8")

    assert canonicalize_json(source) == expected


def test_rfc_appendix_c_address() -> None:
    source = (
        '{"name":"John Doe","address":"2000 Sunset Boulevard",'
        '"city":"Los Angeles","zip":"90001","state":"CA"}'
    )

    assert canonicalize_json(source) == (
        b'{"address":"2000 Sunset Boulevard","city":"Los Angeles",'
        b'"name":"John Doe","state":"CA","zip":"90001"}'
    )


def test_rfc_appendix_e_strings_remain_unmodified() -> None:
    # Appendix E's "val":3.5 is outside this profile; subtype strings are not.
    source = '{"time":"2019-01-28T07:45:10Z","big":"055"}'

    assert canonicalize_json(source) == b'{"big":"055","time":"2019-01-28T07:45:10Z"}'


def test_every_ascii_control_has_its_exact_json_escape() -> None:
    value = "".join(chr(code) for code in range(32)) + '"\\/'
    expected = (
        b'"\\u0000\\u0001\\u0002\\u0003\\u0004\\u0005\\u0006\\u0007'
        b"\\b\\t\\n\\u000b\\f\\r\\u000e\\u000f"
        b"\\u0010\\u0011\\u0012\\u0013\\u0014\\u0015\\u0016\\u0017"
        b'\\u0018\\u0019\\u001a\\u001b\\u001c\\u001d\\u001e\\u001f\\"\\\\/"'
    )

    assert canonicalize(value) == expected


def test_other_unicode_is_utf8_without_extra_escaping_or_bom() -> None:
    value = "\u007f\u0080\u2028\u2029\U0001f600\ufeff"

    assert canonicalize(value) == bytes.fromhex(
        "22 7f c2 80 e2 80 a8 e2 80 a9 f0 9f 98 80 ef bb bf 22"
    )


def test_raw_key_order_handles_prefixes_numbers_and_surrogate_boundaries() -> None:
    source = (
        r'{"\ue000":1,"\ud800\udc00":2,"\udbff\udfff":3,'
        r'"\u0100":4,"\u0080":5,"ab":6,"aa":7,"a":8,"\n":9,"":10,"2":11,"10":12}'
    )
    expected = (
        '{"":10,"\\n":9,"10":12,"2":11,"a":8,"aa":7,"ab":6,'
        '"\u0080":5,"\u0100":4,"\U00010000":2,"\U0010ffff":3,"\ue000":1}'
    ).encode("utf-8")

    assert canonicalize_json(source) == expected


def test_nested_objects_are_sorted_but_semantic_arrays_keep_their_order() -> None:
    value: JSONValue = {
        "z": {"b": False, "a": "123.45"},
        "a": [3, {"b": True, "a": None}, 1],
    }

    result = canonicalize(value)

    assert result == b'{"a":[3,{"a":null,"b":true},1],"z":{"a":"123.45","b":false}}'
    assert hashlib.sha256(result).hexdigest() == (
        "c5183640938e072a15be6fbdb0935686dfe2128c8c87facb92cf2b3ae582d930"
    )
    assert value == {
        "z": {"b": False, "a": "123.45"},
        "a": [3, {"b": True, "a": None}, 1],
    }


def test_insertion_order_does_not_change_bytes() -> None:
    members: list[tuple[str, JSONValue]] = [("b", 2), ("\U0001f600", 3), ("\ufb33", 1)]
    expected = '{"b":2,"\U0001f600":3,"\ufb33":1}'.encode("utf-8")

    for order in permutations(members):
        assert canonicalize(dict(order)) == expected


def test_unicode_normalization_is_not_applied() -> None:
    assert canonicalize(["\u00e9", "e\u0301"]) == bytes.fromhex(
        "5b 22 c3 a9 22 2c 22 65 cc 81 22 5d"
    )


@pytest.mark.parametrize(
    "source",
    [
        '{"a":1,"a":2}',
        r'{"a":1,"\u0061":2}',
        r'{"nested":[{"\ud83d\ude00":1,"\uD83D\uDE00":2}]}',
        '{"a":{"x":1,"x":2}}',
    ],
)
def test_duplicate_decoded_keys_are_rejected_at_the_json_boundary(source: str) -> None:
    with pytest.raises(ValueError, match="Duplicate JSON key"):
        _ = canonicalize_json(source)


@pytest.mark.parametrize(
    "source",
    [
        "1.0",
        "-0.0",
        "1e0",
        "333333333.33333329",
        "1E30",
        "4.50",
        "2e-3",
        "0.000000000000000000000000001",
        "5e-324",
        "-5e-324",
        "1.7976931348623157e+308",
        "1424953923781206.2",
        "1.4e+9999",
        "NaN",
        "Infinity",
        "-Infinity",
        "9007199254740992",
        "-9007199254740992",
        "295147905179352830000",
        "9223372036854775807",
    ],
)
def test_rfc_numbers_excluded_by_the_plan_are_rejected(source: str) -> None:
    with pytest.raises(ValueError, match=r"unsupported|safe domain"):
        _ = canonicalize_json('{"nested":[' + source + "]}")


@pytest.mark.parametrize(
    "value",
    [0.0, -0.0, 1.0, float("nan"), float("inf"), -float("inf"), 2**53, -(2**53)],
)
def test_programmatic_numbers_obey_the_same_profile(value: float) -> None:
    with pytest.raises(ValueError, match=r"unsupported|safe domain"):
        _ = canonicalize({"nested": [value]})


@pytest.mark.parametrize(
    "surrogate",
    [chr(0xD800), chr(0xDFFF), chr(0xDEAD), chr(0xD800) + "x", "x" + chr(0xDFFF)],
)
def test_lone_surrogates_in_strings_and_keys_are_rejected(surrogate: str) -> None:
    with pytest.raises(UnicodeEncodeError):
        _ = canonicalize([surrogate])
    with pytest.raises(UnicodeEncodeError):
        _ = canonicalize({surrogate: None})


@pytest.mark.parametrize(
    "source",
    [
        b"\xef\xbb\xbf{}",
        b"\xff\xfe{\x00}\x00",
        b"\x00{\x00}",
        b'"\xff"',
        b'"\xed\xa0\x80"',
        rb'"\ud800"',
        rb'{"\udfff":0}',
        b"",
        b"{}{}",
        b'{"a":01}',
        b'{"a":1,}',
        b'{"a":"\x00"}',
    ],
)
def test_invalid_json_encoding_and_syntax_are_rejected(source: bytes) -> None:
    with pytest.raises((JSONDecodeError, UnicodeError)):
        _ = canonicalize_json(source)


@pytest.mark.parametrize(
    ("value", "scale", "rounding", "expected"),
    [
        ("123.45", 2, ROUND_HALF_EVEN, "123.45"),
        ("1.2", 2, ROUND_HALF_EVEN, "1.20"),
        ("1E+3", 2, ROUND_HALF_EVEN, "1000.00"),
        ("1.005", 2, ROUND_HALF_EVEN, "1.00"),
        ("1.005", 2, ROUND_HALF_UP, "1.01"),
        ("-1.009", 2, ROUND_DOWN, "-1.00"),
        ("9.995", 2, ROUND_HALF_EVEN, "10.00"),
        ("1.2345", 3, ROUND_HALF_UP, "1.235"),
        ("2.5", 0, ROUND_HALF_EVEN, "2"),
        ("-0", 2, ROUND_HALF_EVEN, "-0.00"),
        ("1E-30", 2, ROUND_HALF_EVEN, "0.00"),
        (
            "123456789012345678901234567890.12",
            2,
            ROUND_HALF_EVEN,
            "123456789012345678901234567890.12",
        ),
    ],
)
def test_decimal_strings_have_explicit_scale_and_rounding(
    value: str, scale: int, rounding: str, expected: str
) -> None:
    assert decimal_string(Decimal(value), scale=scale, rounding=rounding) == expected


def test_decimal_formatting_is_independent_of_ambient_context() -> None:
    with localcontext() as context:
        context.prec = 1
        context.rounding = ROUND_DOWN
        context.Emax = 1
        context.Emin = -1
        for signal in context.traps:
            context.traps[signal] = True

        result = decimal_string(Decimal("999.995"), scale=2, rounding=ROUND_HALF_EVEN)
        assert result == "1000.00"
        assert context.prec == 1
        assert not any(context.flags.values())


@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_nonfinite_decimals_are_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="finite Decimal"):
        _ = decimal_string(Decimal(value), scale=2, rounding=ROUND_HALF_EVEN)


def test_negative_decimal_scale_is_rejected() -> None:
    with pytest.raises(ValueError, match="nonnegative scale"):
        _ = decimal_string(Decimal("123.45"), scale=-1, rounding=ROUND_HALF_EVEN)
