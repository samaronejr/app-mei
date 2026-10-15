"""One way to hand stored client bytes back to a browser (S8.3 "Previews").

Every served document carries:

- `Content-Security-Policy: sandbox` -- even a file a browser decides to render runs in
  an opaque origin with scripts, forms and same-origin access off (stored XSS);
- `X-Content-Type-Options: nosniff` -- the declared type is final;
- a `Content-Type` from the allowlist the upload sniffer proved, never what a client
  declared; anything else is `application/octet-stream`;
- a `Content-Disposition` built by Django's RFC 6266/5987 helper (`filename*=UTF-8''`)
  from a name stripped of control characters and path separators, so a file name can
  neither inject a header nor carry a path.

Never a streaming response (see `PortalMiddleware._reject_streaming`).
"""

import unicodedata
from typing import Final

from django.http import HttpResponse
from django.utils.http import content_disposition_header

from apps.cycles.sniffing import ALLOWED_TYPES

SANDBOX_POLICY: Final = "sandbox"
FALLBACK_NAME: Final = "documento"
FALLBACK_TYPE: Final = "application/octet-stream"
NAME_MAX: Final = 180


def safe_filename(name: str) -> str:
    """Drop control/format characters and path separators; never empty."""
    cleaned = "".join(
        " " if char in "/\\" else char
        for char in name
        if unicodedata.category(char) not in {"Cc", "Cf", "Zl", "Zp"}
    ).strip(" .")
    return cleaned[:NAME_MAX] or FALLBACK_NAME


def document_response(
    payload: bytes,
    *,
    content_type: str,
    filename: str,
    inline: bool = False,
) -> HttpResponse:
    """Wrap bytes in the sandboxed, non-sniffable, safely named response."""
    served_type = content_type if content_type in ALLOWED_TYPES else FALLBACK_TYPE
    response = HttpResponse(payload, content_type=served_type)
    disposition = content_disposition_header(
        as_attachment=not inline, filename=safe_filename(filename)
    )
    response["Content-Disposition"] = disposition or "attachment"
    response["Content-Security-Policy"] = SANDBOX_POLICY
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response
