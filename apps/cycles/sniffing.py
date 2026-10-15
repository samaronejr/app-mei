"""What an uploaded file really is, decided from its bytes alone (S8.3, M5).

The declared content type and the file name are the uploader's claims and are never
consulted. `filetype` recognises the binary formats by their magic numbers; XML has
none, so it is recognised by its first bytes. Only four types pass (PDF, XML, PNG,
JPEG); everything else -- ZIP and the Office formats built on it, HTML, SVG, HEIC,
executables, unknown bytes -- is refused as `rejeitado_tipo`.

Sniffing is not parsing: nothing here builds a tree or follows a reference. Parsing
waits for a clean verdict (`apps.cycles.parsing`).

Polyglots are refused. A browser that ignores the declared type sniffs markup, so a
file that begins like a PDF or an image but carries HTML/SVG markup anywhere is refused
even though its header is allowed. The serving headers (`nosniff`, CSP `sandbox`) are
the second line; this is the first.
"""

import re
from typing import Final

import filetype

PDF: Final = "application/pdf"
XML: Final = "application/xml"
PNG: Final = "image/png"
JPEG: Final = "image/jpeg"
ALLOWED_TYPES: Final = frozenset({PDF, XML, PNG, JPEG})

# First-bytes confirmation of what `filetype` reports: the magic must sit at offset 0.
# PDF readers accept junk before `%PDF-` (within 1 KiB), which is exactly the room a
# polyglot needs, so the header is required at the very start.
_MAGIC: Final[dict[str, bytes]] = {
    PDF: b"%PDF-",
    PNG: b"\x89PNG\r\n\x1a\n",
    JPEG: b"\xff\xd8\xff",
}
_UTF8_BOM: Final = b"\xef\xbb\xbf"
_XML_START: Final = re.compile(rb"\A(?:<\?xml[\s?]|<[A-Za-z_][\w.:-]*[\s/>])")
# Markup a browser would render or execute, matched case-insensitively. Text (XML) is
# searched whole for every marker. Binary formats are searched with every marker over
# the sniffing window browsers use (WHATWG: 1445 bytes; 4 KiB here) and with only the
# long, unambiguous markers over the whole payload -- a four-byte `<svg` occurs by
# chance in megabytes of compressed image data, `<script` practically never does.
_ACTIVE_MARKUP: Final = re.compile(
    rb"<(?:!doctype\s+html|html|head|body|script|iframe|object|embed|svg|"
    rb"foreignobject|meta\s|link\s|form|img\s|a\s+href)|"
    rb"xmlns\s*=\s*[\"']http://www\.w3\.org/(?:1999/xhtml|2000/svg)",
    re.IGNORECASE,
)
_LONG_MARKUP: Final = re.compile(
    rb"<(?:!doctype\s+html|script|iframe|object\s|embed\s|foreignobject)|"
    rb"xmlns\s*=\s*[\"']http://www\.w3\.org/(?:1999/xhtml|2000/svg)",
    re.IGNORECASE,
)
SNIFF_WINDOW: Final = 4096


def _looks_like_xml(payload: bytes) -> bool:
    head = payload.removeprefix(_UTF8_BOM).lstrip(b" \t\r\n")
    return bool(_XML_START.match(head[:256]))


def sniff(payload: bytes) -> str | None:
    """Return the allowed MIME type the bytes prove, or None to refuse."""
    if not payload:
        return None
    kind = filetype.guess(payload)
    if kind is not None:
        mime = str(kind.mime)
        magic = _MAGIC.get(mime)
        if (
            magic is None
            or not payload.startswith(magic)
            or _ACTIVE_MARKUP.search(payload[:SNIFF_WINDOW])
            or _LONG_MARKUP.search(payload)
        ):
            return None
        return mime
    if _looks_like_xml(payload) and not _ACTIVE_MARKUP.search(payload):
        return XML
    return None
