"""CNPJ and CPF validation, implemented from Receita Federal's published algorithm.

The algorithm and its primary sources are recorded in `docs/fiscal/cnpj-alfanumerico.md`
and verified in `.evidence/T-031-*.txt`. This module implements it; it does not
re-derive it.

Two properties of the published algorithm are surprising enough that they are stated
here rather than left for a reader to discover:

**Módulo 11 over a 36-character alphabet is not injective.** Character values are
`ord(c) - 48`, spanning 0..42, which is wider than the modulus. Characters whose values
are congruent mod 11 are interchangeable in any of the twelve base positions without
changing either check digit — `12.LBC.345/01DE-35` is a valid CNPJ derived from RFB's
own worked example by A→L, and RFB's reference implementation agrees. Nothing here may
"fix" that: those numbers are issued and accepted by the Receita.

**The published arithmetic accepts degenerate inputs.** `00000000000000` satisfies its
own checksum, and *every* repeated-digit CPF satisfies its own. They are excluded by
the explicit rule in `_reject_degenerate`, which is an addition to the published
algorithm made deliberately. Deleting it would not fail any checksum test.

CPF is deliberately NOT alphanumeric. IN RFB nº 2.229/2024 changes the CNPJ only, so
`validate_cpf` refuses letters that `validate_cnpj` accepts. CPF also uses a different
weight range — its weights do not restart at 9 — so the two generators are separate on
purpose and must not be merged.
"""

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

# The 36 characters a CNPJ position may hold. Values 10..16 are unused: they would
# correspond to the ASCII characters between '9' (57) and 'A' (65), none of which is
# permitted.
CNPJ_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
CPF_ALPHABET = "0123456789"

CNPJ_LENGTH = 14
CNPJ_BASE_LENGTH = 12
CPF_LENGTH = 11
CPF_BASE_LENGTH = 9

# Characters a person may type as separators. Stripped before anything else looks at
# the value, so a masked paste and a bare one reach the same code path.
MASK_CHARACTERS = frozenset(".-/ \t\n\r")

_MODULUS = 11
# Below this remainder the digit is zero rather than `11 - remainder`, which would
# otherwise produce the two-character values 10 and 11.
_MIN_REMAINDER = 2
_FIRST_WEIGHT = 2

# CNPJ weights cycle 2..9, restarting after the eighth character. CPF's do not
# restart: over nine and ten characters they run 2..10 and 2..11 exactly. Expressing
# that as a higher cap keeps one generator honest for both without pretending the two
# schemes are the same.
_CNPJ_MAX_WEIGHT = 9
_CPF_MAX_WEIGHT = 11

# The ASCII offset RFB specifies: "Subtraindo 48 temos o valor 17" for 'A'.
_ASCII_OFFSET = 48


def normalize_document(value: str) -> str:
    """Return `value` with mask characters removed and letters uppercased.

    This is the single definition of "normalized" for the whole product. Storage,
    search, comparison and export all route through it, so a masked CNPJ typed into a
    search box matches a record stored bare.
    """
    return "".join(char for char in value if char not in MASK_CHARACTERS).upper()


def _character_value(char: str) -> int:
    """Return the módulo-11 input value for one character: its ASCII code minus 48."""
    return ord(char) - _ASCII_OFFSET


def _check_digit(operand: str, max_weight: int) -> int:
    """Compute one módulo-11 check digit over `operand`.

    Weights ascend from 2, are applied right to left, and restart at 2 once
    `max_weight` is passed. The result is always 0..9, which is why a check digit can
    never be a letter.
    """
    total = 0
    weight = _FIRST_WEIGHT
    for char in reversed(operand):
        total += _character_value(char) * weight
        weight = _FIRST_WEIGHT if weight == max_weight else weight + 1
    remainder = total % _MODULUS
    return 0 if remainder < _MIN_REMAINDER else _MODULUS - remainder


def _both_check_digits(base: str, max_weight: int) -> str:
    """Compute both check digits, the second over the base plus the first."""
    first = _check_digit(base, max_weight)
    second = _check_digit(base + str(first), max_weight)
    return f"{first}{second}"


def cnpj_check_digits(base: str) -> str:
    """Return the two check digits for a CNPJ base, as a two-character string."""
    return _both_check_digits(base, _CNPJ_MAX_WEIGHT)


def cpf_check_digits(base: str) -> str:
    """Return the two check digits for a CPF base, as a two-character string."""
    return _both_check_digits(base, _CPF_MAX_WEIGHT)


def _reject_wrong_length(value: str, expected: int, code: str) -> None:
    if len(value) != expected:
        message = _(
            "%(document)s must be exactly %(expected)s characters once punctuation "
            "is removed; this one has %(actual)s.",
        )
        raise ValidationError(
            message,
            code=code,
            params={
                "document": "A CNPJ" if expected == CNPJ_LENGTH else "A CPF",
                "expected": expected,
                "actual": len(value),
            },
        )


def _reject_bad_characters(value: str, alphabet: str, code: str) -> None:
    offending = sorted({char for char in value if char not in alphabet})
    if offending:
        message = _(
            "%(document)s may contain only %(allowed)s; found %(offending)s.",
        )
        raise ValidationError(
            message,
            code=code,
            params={
                "document": "A CNPJ" if alphabet == CNPJ_ALPHABET else "A CPF",
                "allowed": (
                    "digits and uppercase letters"
                    if alphabet == CNPJ_ALPHABET
                    else "digits"
                ),
                "offending": ", ".join(repr(char) for char in offending),
            },
        )


def _reject_degenerate(value: str, code: str) -> None:
    """Refuse a document made of one repeated character.

    This is an addition to the published algorithm, not part of it. `00000000000000`
    and every repeated-digit CPF satisfy their own checksums, so without this rule they
    would be accepted as valid documents. They are placeholders and typing accidents,
    never real registrations.
    """
    if len(set(value)) == 1:
        message = _(
            "%(value)s is not a real registration: a document cannot be a single "
            "character repeated.",
        )
        raise ValidationError(message, code=code, params={"value": value})


def _reject_non_numeric_check_digits(value: str, base_length: int, code: str) -> None:
    r"""Refuse a CNPJ whose check digits contain a letter.

    The remainder rule can only yield 0..9, so a lettered check digit cannot have been
    produced by the algorithm. RFB's own reference implementation writes this into its
    format regex, where the body accepts `[A-Z]|\d` but the check digits are `\d{2}`.
    """
    digits = value[base_length:]
    if not digits.isdigit():
        message = _(
            "A CNPJ's two check digits are always numeric; this one ends in "
            "%(digits)s.",
        )
        raise ValidationError(message, code=code, params={"digits": digits})


def validate_cnpj(value: str) -> None:
    """Validate a CNPJ in either the alphanumeric or the legacy all-numeric format.

    Accepts masked, unmasked and lowercase input — the value is normalized first.
    Raises `ValidationError` with a distinct `code` for each way of being wrong, so a
    caller can tell a mistyped digit from a truncated paste.

    Both formats are validated by this one function. IN RFB nº 2.229/2024 is backward
    compatible: an existing numeric CNPJ validates unchanged, and there is deliberately
    not a second code path that could drift from this one.
    """
    normalized = normalize_document(value)
    _reject_wrong_length(normalized, CNPJ_LENGTH, "cnpj_length")
    _reject_bad_characters(normalized, CNPJ_ALPHABET, "cnpj_characters")
    _reject_degenerate(normalized, "cnpj_degenerate")
    _reject_non_numeric_check_digits(
        normalized,
        CNPJ_BASE_LENGTH,
        "cnpj_check_digit_characters",
    )

    base, given = normalized[:CNPJ_BASE_LENGTH], normalized[CNPJ_BASE_LENGTH:]
    expected = cnpj_check_digits(base)
    if given != expected:
        message = _(
            "The check digits of %(value)s are wrong: they should be %(expected)s.",
        )
        raise ValidationError(
            message,
            code="cnpj_check_digits",
            params={"value": normalized, "expected": expected},
        )


def validate_cpf(value: str) -> None:
    """Validate a CPF.

    CPF is numeric-only. The alphanumeric change applies to the CNPJ alone, so a letter
    here is an error even though the same letter is ordinary inside a CNPJ.
    """
    normalized = normalize_document(value)
    _reject_wrong_length(normalized, CPF_LENGTH, "cpf_length")
    _reject_bad_characters(normalized, CPF_ALPHABET, "cpf_characters")
    _reject_degenerate(normalized, "cpf_degenerate")

    base, given = normalized[:CPF_BASE_LENGTH], normalized[CPF_BASE_LENGTH:]
    expected = cpf_check_digits(base)
    if given != expected:
        message = _(
            "The check digits of %(value)s are wrong: they should be %(expected)s.",
        )
        raise ValidationError(
            message,
            code="cpf_check_digits",
            params={"value": normalized, "expected": expected},
        )
