"""Provider credential recognition for the existing console and Sentry scrubbers."""

import re
from collections.abc import Mapping
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
# `name: value`, `name=value` and rendered pairs such as `('name', 'value')` or
# `(b'name', b'value')`. An already-redacted value is not captured again.
_FIELD_VALUE: Final = re.compile(
    r"""(?ix)(?:authorization|proxy-authorization|jwt_token|access_token|
    autenticar_procurador_token|x-[a-z0-9_-]*token[a-z0-9_-]*)
    ["']?\s*[:=,]\s*(?:b?["'])?(?:Bearer\s+|Basic\s+)?
    (?!\[Filtered\])([^\s"',;}\])]+)"""
)
_BEARER: Final = re.compile(r"(?i)\bBearer\s+([A-Za-z0-9._~+/=-]+)")
# A quoted body field rendered from a mapping: `'body': 'value'`.
_QUOTED_BODY: Final = re.compile(
    r"""((["'])(?:body|data|payload)\2\s*:\s*(["']))(?:(?!\3).)*\3""",
    re.IGNORECASE,
)
# Shorter collected values would be replaced inside unrelated words.
MIN_COLLECTED: Final = 8
_QUERY: Final = re.compile(r"(?i)([?&](?:token|access_token|jwt_token)=)([^&#\s\"']+)")
_SIGNED_URL: Final = re.compile(
    r"""https?://[^\s"'<>]*[?&](?:X-Amz-[^=\s]+|Signature|AWSAccessKeyId)=[^\s"'<>]*""",
    re.IGNORECASE,
)
# Presigned query credentials, also when logged without scheme and host
# (urllib3 logs `"GET /file?X-Amz-Signature=... HTTP/1.1"`).
_SIGNED_QUERY: Final = re.compile(
    r"(?i)([?&](?:X-Amz-[A-Za-z-]+|Signature|AWSAccessKeyId)=)[^&#\s\"']+"
)
_BASE64: Final = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{1025,}={0,2}")


def provider_field(name: str | bytes) -> bool:
    """Recognize named tokens and all provider X-*Token* header variants."""
    lowered = (name.decode("latin-1") if isinstance(name, bytes) else name).lower()
    return lowered in PROVIDER_FIELDS or (
        lowered.startswith("x-") and "token" in lowered
    )


def scrub_provider_text(value: str) -> str:
    """Remove credential-bearing text even without an attached Django request."""
    value = _SIGNED_URL.sub(MARKER, value)
    value = _FIELD_VALUE.sub(lambda match: match[0].replace(match[1], MARKER), value)
    value = _BEARER.sub(lambda match: match[0].replace(match[1], MARKER), value)
    value = _QUOTED_BODY.sub(rf"\1{MARKER}\3", value)
    value = _SIGNED_QUERY.sub(rf"\1{MARKER}", value)
    value = _QUERY.sub(lambda match: match[1] + MARKER, value)
    return _BASE64.sub(MARKER, value)


def scrub_text(value: str, credentials: set[str]) -> str:
    """Replace every active credential value, then provider credential shapes."""
    for credential in sorted(credentials, key=len, reverse=True):
        value = value.replace(credential, MARKER)
    return scrub_provider_text(value)


def collect_credentials(value: object, credentials: set[str]) -> None:
    """Remember credential values long enough to replace them elsewhere."""
    if isinstance(value, bytes):
        collect_credentials(value.decode("utf-8", "replace"), credentials)
    elif isinstance(value, str):
        for candidate in (value, _BEARER.sub(r"\1", value)):
            if len(candidate) >= MIN_COLLECTED:
                credentials.add(candidate)
    elif isinstance(value, Mapping):
        for item in value.values():
            collect_credentials(item, credentials)
    elif isinstance(value, (list, tuple)):
        for item in value:
            collect_credentials(item, credentials)


def credential_pair(value: object) -> bool:
    """Recognize a header-style `(name, value)` pair naming a provider credential."""
    return (
        isinstance(value, (list, tuple))
        and len(value) == 2  # noqa: PLR2004
        and isinstance(value[0], (str, bytes))
        and provider_field(value[0])
    )


def redact_structure(value: object, credentials: set[str]) -> object:
    """Redact before rendering, while field names and header pairs still exist.

    Values found under provider fields are added to `credentials`, so a later
    `scrub_text` pass also removes their other occurrences.
    """
    if isinstance(value, Mapping):
        redacted: dict[object, object] = {}
        for key, item in value.items():
            if isinstance(key, (str, bytes)) and provider_field(key):
                collect_credentials(item, credentials)
                redacted[key] = MARKER
            else:
                redacted[key] = redact_structure(item, credentials)
        return redacted
    if isinstance(value, (list, tuple)):
        if credential_pair(value):
            collect_credentials(value[1], credentials)
            items: list[object] = [value[0], MARKER]
        else:
            items = [redact_structure(item, credentials) for item in value]
        return tuple(items) if isinstance(value, tuple) else items
    if isinstance(value, str):
        # Known values first: pattern rules would alter them and defeat the match.
        return scrub_text(value, credentials)
    return value
