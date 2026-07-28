"""Display masks for the documents this product stores normalized.

Storage and display are separated on purpose. A masked value in the column would make
every comparison depend on which punctuation the typist used, and a search for
`12ABC34501DE35` would miss `12.ABC.345/01DE-35`. The column holds the bare characters
and these helpers put the mask back on the way out.
"""

from apps.fiscal.validators import (
    CNPJ_LENGTH,
    CPF_LENGTH,
    normalize_document,
)

# The official CNPJ mask is AA.AAA.AAA/AAAA-DD: two, three, three, four, then the two
# numeric check digits. RFB's own reference implementation encodes the same grouping.
_CNPJ_GROUPS = (2, 3, 3, 4, 2)
_CNPJ_SEPARATORS = (".", ".", "/", "-")

_CPF_GROUPS = (3, 3, 3, 2)
_CPF_SEPARATORS = (".", ".", "-")


def _apply_mask(
    value: str,
    expected_length: int,
    groups: tuple[int, ...],
    separators: tuple[str, ...],
) -> str:
    normalized = normalize_document(value)
    if len(normalized) != expected_length:
        msg = (
            f"{value!r} is not {expected_length} characters once normalized, so it "
            f"cannot be masked. Values reaching a display helper have already passed "
            f"validation and the database CHECK constraint."
        )
        raise ValueError(msg)

    parts = []
    cursor = 0
    for size in groups:
        parts.append(normalized[cursor : cursor + size])
        cursor += size

    masked = parts[0]
    for separator, part in zip(separators, parts[1:], strict=True):
        masked += separator + part
    return masked


def format_cnpj(value: str) -> str:
    """Return `value` in the official CNPJ mask, `AA.AAA.AAA/AAAA-DD`.

    Works unchanged for the legacy all-numeric format, which uses the same grouping.
    """
    return _apply_mask(value, CNPJ_LENGTH, _CNPJ_GROUPS, _CNPJ_SEPARATORS)


def format_cpf(value: str) -> str:
    """Return `value` in the official CPF mask, `AAA.AAA.AAA-DD`."""
    return _apply_mask(value, CPF_LENGTH, _CPF_GROUPS, _CPF_SEPARATORS)
