"""`secret_ref` holds the NAME of a deployment secret, never the secret itself.

ADR-0008 / INV-R7: provider credentials are deployment secrets referenced by name. A
name is lower-case words joined by `_`, `.`, `-` or `/` (`serpro_contratante`,
`focus/token_master`). Anything else -- upper case (JWTs, base64), spaces (`Bearer`),
`:` and `@` (URLs carrying credentials), `-----BEGIN` (PEM) or one long unbroken run
of characters (hex digests, tokens) -- is the shape of a value, and is refused. The
same pattern is a database CHECK (`providerconnection_secret_ref_is_a_name`), so a
write that skips model validation is refused too.
"""

import re
from typing import Final

from django.core.exceptions import ValidationError

REF_MAX_LENGTH: Final = 64
# Lower-case words joined by single separators, starting with a letter.
REF_NAME_PATTERN: Final = r"^[a-z][a-z0-9]*([_./-][a-z0-9]+)*$"
# One unbroken run this long is a token or a digest, not a word in a name.
REF_VALUE_RUN_PATTERN: Final = r"[a-z0-9]{25,}"

_NAME = re.compile(REF_NAME_PATTERN)
_VALUE_RUN = re.compile(REF_VALUE_RUN_PATTERN)


def validate_secret_ref(value: str) -> None:
    """Refuse anything shaped like a secret value; blank means "not configured"."""
    if value == "":
        return
    if (
        len(value) > REF_MAX_LENGTH
        or not _NAME.fullmatch(value)
        or _VALUE_RUN.search(value)
    ):
        message = (
            "Informe apenas o nome do segredo (por exemplo, serpro_contratante), "
            "nunca o valor da credencial."
        )
        raise ValidationError(message, code="secret_value")
