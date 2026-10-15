"""One prompt and one fail-closed output boundary for eval and shadow runs."""

import json
import re
import unicodedata
from pathlib import Path
from typing import Final
from urllib.parse import unquote

from apps.automation.types import (
    Classification,
    DocType,
    Field,
    JSONObject,
    JSONValue,
    Message,
    UnsafeOutputError,
)
from apps.cycles.extraction.cnpj import party
from apps.fiscal.validators import normalize_document

VERSION: Final = "doc_classify_v1"
PROMPTS: Final = Path(__file__).parent / "prompts"
MAX_OUTPUT: Final = 16384
MAX_FIELDS: Final = 20
URL: Final = re.compile(
    r"(?:[a-z][a-z0-9+.-]*://|//|www\.|"
    r"\b(?:https?|ftp|file|mailto|data|javascript|tel|wss?|gopher|urn|ssh|smb):|"
    r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}(?:\b|/)|"
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b)",
    re.IGNORECASE,
)
# NFKC leaves the ideographic full stop; map every dot variant to ASCII.
DOTS: Final = str.maketrans(dict.fromkeys("\u3002\uff0e\uff61\ufe52", "."))
# A host in any script: letter/digit labels joined by dots and ending in a
# label of two or more letters, e.g. exemplo.\u0440\u0444 or \u4f8b\u5b50.\u6d4b\u8bd5.
LABEL: Final = r"[^\W_](?:[\w-]*[^\W_])?"
WEB_HOST: Final = re.compile(
    rf"(?<![\w-]){LABEL}(?:\.{LABEL})*\.[^\W\d_]{{2,}}(?![\w-])"
)
CANDIDATE: Final = re.compile(r"\S+")
IDENTIFIER: Final = re.compile(
    r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}",
    re.IGNORECASE,
)
OUTPUT_CNPJ: Final = re.compile(
    r"(?=([0-9A-Z]{2}\.?[0-9A-Z]{3}\.?[0-9A-Z]{3}/?[0-9A-Z]{4}-?[0-9]{2}))"
)


def schema() -> JSONObject:
    """Read the versioned, shipped provider schema."""
    value: JSONValue = json.loads((PROMPTS / f"{VERSION}.schema.json").read_text())
    if not isinstance(value, dict):
        msg = "Classification schema must be an object."
        raise TypeError(msg)
    return value


def messages_for(
    client_cnpj: str, text: str, expected_documents: tuple[str, ...] = ()
) -> tuple[Message, ...]:
    """Wrap untrusted text as JSON data, separate from system instructions."""
    return (
        {"role": "system", "content": (PROMPTS / f"{VERSION}.md").read_text()},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "client_cnpj": client_cnpj,
                    "expected_documents": expected_documents,
                    "document_data": text,
                },
                ensure_ascii=False,
            ),
        },
    )


def _unique(pairs: list[tuple[str, JSONValue]]) -> JSONObject:
    result: JSONObject = {}
    for key, value in pairs:
        if key in result:
            message = "duplicate_key"
            raise UnsafeOutputError(message)
        result[key] = value
    return result


def _string(value: JSONValue, maximum: int) -> str:
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        message = "invalid_string"
        raise UnsafeOutputError(message)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        message = "invalid_unicode"
        raise UnsafeOutputError(message) from error
    return value


def _idna_reference(text: str) -> bool:
    """Run the ASCII reference check on the punycode form of non-ASCII hosts."""
    for candidate in CANDIDATE.findall(text):
        if candidate.isascii() or "." not in candidate:
            continue
        try:
            encoded = candidate.encode("idna").decode("ascii")
        except UnicodeError:
            continue
        if URL.search(encoded):
            return True
    return False


def _safe(value: str, client_cnpj: str) -> None:
    normalized = unicodedata.normalize("NFKC", unquote(value))
    normalized = "".join(
        char for char in normalized if unicodedata.category(char) != "Cf"
    ).translate(DOTS)
    if (
        URL.search(normalized)
        or WEB_HOST.search(normalized)
        or _idna_reference(normalized)
        or IDENTIFIER.search(normalized)
    ):
        message = "external_reference"
        raise UnsafeOutputError(message)
    # Output has a stricter boundary than document recognition: a label/prefix
    # cannot hide an identity. Overlapping candidates cover attached identifiers.
    for match in OUTPUT_CNPJ.finditer(normalized.upper()):
        cnpj = normalize_document(match[1])
        if cnpj != client_cnpj and party(cnpj):
            message = "other_client"
            raise UnsafeOutputError(message)


def _fields(entries: JSONValue, client_cnpj: str) -> tuple[Field, ...]:
    if not isinstance(entries, list) or len(entries) > MAX_FIELDS:
        message = "invalid_fields"
        raise UnsafeOutputError(message)
    fields: list[Field] = []
    names: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "name",
            "value",
            "source_span",
        }:
            message = "invalid_field"
            raise UnsafeOutputError(message)
        name = _string(entry["name"], 64)
        content = _string(entry["value"], 255)
        span = _string(entry["source_span"], 255)
        if (
            name not in {"competencia", "numero", "valor_total", "cnpj"}
            or name in names
        ):
            message = "invalid_field_name"
            raise UnsafeOutputError(message)
        for part in (name, content, span):
            _safe(part, client_cnpj)
        names.add(name)
        fields.append(Field(name, content, span))
    return tuple(fields)


def validate_output(raw: str, client_cnpj: str) -> Classification:
    """Parse the fixed schema and reject references in every textual position."""
    if len(raw) > MAX_OUTPUT:
        message = "output_too_large"
        raise UnsafeOutputError(message)
    try:
        value: JSONValue = json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, RecursionError) as error:
        message = "invalid_json"
        raise UnsafeOutputError(message) from error
    keys = {"doc_type", "fields", "client_cnpj_seen", "confidence_band"}
    if not isinstance(value, dict) or set(value) != keys:
        message = "invalid_properties"
        raise UnsafeOutputError(message)
    kind = _string(value["doc_type"], 32)
    confidence = _string(value["confidence_band"], 16)
    cnpj = _string(value["client_cnpj_seen"], 14)
    if kind not in DocType:
        message = "invalid_doc_type"
        raise UnsafeOutputError(message)
    if confidence not in {"high", "medium", "low", "abstain"}:
        message = "invalid_confidence"
        raise UnsafeOutputError(message)
    if cnpj not in {"", client_cnpj}:
        message = "other_client"
        raise UnsafeOutputError(message)
    fields = _fields(value["fields"], client_cnpj)
    if confidence == "abstain" and (fields or cnpj):
        message = "invalid_abstention"
        raise UnsafeOutputError(message)
    if confidence != "abstain" and cnpj != client_cnpj:
        message = "missing_attribution"
        raise UnsafeOutputError(message)
    return Classification(kind, tuple(fields), cnpj, confidence)
