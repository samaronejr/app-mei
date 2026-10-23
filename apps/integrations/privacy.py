"""Provider credential recognition for the existing console and Sentry scrubbers."""

import re
from typing import Final

MARKER: Final = "[Filtered]"
PROVIDER_FIELDS: Final = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "jwt_token",
        "access_token",
        "autenticar_procurador_token",
        "token",
        "body",
        "data",
        "payload",
    }
)
_FIELD_VALUE: Final = re.compile(
    r"""(?ix)(?:authorization|proxy-authorization|jwt_token|access_token|
    autenticar_procurador_token|x-[a-z0-9_-]*token[a-z0-9_-]*)
    ["']?\s*[:=]\s*["']?(?:Bearer\s+|Basic\s+)?([^\s"',;}\]]+)"""
)
_QUERY: Final = re.compile(r"(?i)([?&](?:token|access_token|jwt_token)=)([^&#\s\"']+)")
_SIGNED_URL: Final = re.compile(
    r"""https?://[^\s"'<>]*[?&](?:X-Amz-[^=\s]+|Signature|AWSAccessKeyId)=[^\s"'<>]*""",
    re.IGNORECASE,
)
_BASE64: Final = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{1025,}={0,2}")


def provider_field(name: str) -> bool:
    """Recognize named tokens and all provider X-*Token* header variants."""
    lowered = name.lower()
    return lowered in PROVIDER_FIELDS or (
        lowered.startswith("x-") and "token" in lowered
    )


def scrub_provider_text(value: str) -> str:
    """Remove credential-bearing text even without an attached Django request."""
    value = _SIGNED_URL.sub(MARKER, value)
    value = _FIELD_VALUE.sub(lambda match: match[0].replace(match[1], MARKER), value)
    value = _QUERY.sub(lambda match: match[1] + MARKER, value)
    return _BASE64.sub(MARKER, value)
