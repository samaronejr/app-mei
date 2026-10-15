"""CNPJ recognition for extraction: the product's own validator, alphanumeric included.

`docs/fiscal/cnpj-alfanumerico.md` records the algorithm (IN RFB 2.229/2024);
`apps.fiscal.validators.validate_cnpj` implements it. Only a value that passes it
counts as a party of the document -- a mistyped or random 14-character run is text,
not a company.
"""

import re
from collections.abc import Iterator
from typing import Final

from django.core.exceptions import ValidationError

from apps.fiscal.validators import normalize_document, validate_cnpj

# Twelve alphanumeric base characters and two numeric check digits, optionally masked
# (12.ABC.345/01DE-35). The lookarounds refuse a run inside a longer one (an invoice
# access key is 44 digits), so only a standalone number is a candidate.
CANDIDATE: Final = re.compile(
    r"(?<![0-9A-Z])"
    r"([0-9A-Z]{2}\.?[0-9A-Z]{3}\.?[0-9A-Z]{3}/?[0-9A-Z]{4}-?[0-9]{2})"
    r"(?![0-9A-Z])"
)


def party(value: str) -> bool:
    """Whether a normalized value is a valid CNPJ."""
    try:
        validate_cnpj(value)
    except ValidationError:
        return False
    return True


def find(text: str) -> Iterator[tuple[str, int, int]]:
    """Yield (normalized CNPJ, start, end) for every valid CNPJ in the text."""
    for match in CANDIDATE.finditer(text):
        value = normalize_document(match.group(1))
        if party(value):
            yield value, match.start(1), match.end(1)
