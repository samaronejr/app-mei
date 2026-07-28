"""CSV export of the client registry, built in memory and never streamed.

**This export must not become a `StreamingHttpResponse`, and `TenantMiddleware` raises
`TypeError` if it does.** A streaming iterator is consumed by the WSGI server *after*
every middleware has returned, by which time the tenant transaction has committed and
the `app.tenant_id` GUC is gone. Every lazy query inside the generator would then run
under the fail-closed row-level-security predicate and return **zero rows**, so the
user would download a file that succeeded, weighed a few bytes, and silently contained
none of their clients. The decision to forbid streaming here was made in T-011; this
module is where it is honoured.

Two encoding hazards are handled here rather than left to whatever opens the file.

**Numeric coercion.** A CNPJ such as `00000000000191` is not a number, but a
spreadsheet will treat it as one, drop the leading zeros, and render a long digit
string in scientific notation. Every field is therefore quoted. The `="..."` trick
that also defeats coercion is deliberately **not** used: it is a formula-injection
vector, and it would stop the value round-tripping byte for byte.

**Formula injection.** A cell beginning `=`, `+`, `-` or `@` is executed as a formula
by Excel and LibreOffice, so a client named `=cmd|'/c calc'!A1` would run on the
accountant's machine. Free-text columns are defused with a leading apostrophe. Document
columns need no defusing and receive none, because the CNPJ/CPF alphabet is
`0-9A-Z` and cannot begin with any of those characters — which is what keeps the
byte-for-byte round-trip intact. A test asserts that invariant rather than trusting it.
"""

import csv
import io
from collections.abc import Iterable, Sequence

from apps.clients.models.company import ClientCompany

# Document columns first: they are what the pack in T-034 round-trips, and putting
# them at the front makes a truncated file obviously truncated.
EXPORT_COLUMNS: Sequence[str] = (
    "cnpj",
    "cpf",
    "legal_name",
    "trade_name",
    "status",
    "municipality_ibge_code",
    "state",
    "main_cnae",
)

# Columns holding a normalized document. Structurally immune to formula injection, and
# therefore never rewritten, so re-import returns exactly what was stored.
DOCUMENT_COLUMNS: frozenset[str] = frozenset({"cnpj", "cpf"})

# The characters a spreadsheet treats as the start of a formula.
FORMULA_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")

# Excel reads a UTF-8 CSV as the system codepage unless a byte-order mark says
# otherwise, which turns every accented Brazilian legal name into mojibake. The mark
# is added by the view, not here, so this function's output stays pure CSV and the
# round-trip test reads what was written.
UTF8_BOM = "\ufeff"


def defuse_formula(value: str) -> str:
    """Neutralize a value a spreadsheet would otherwise execute as a formula."""
    if value.startswith(FORMULA_PREFIXES):
        return "'" + value
    return value


def _cell(column: str, value: object) -> str:
    text = "" if value is None else str(value)
    if column in DOCUMENT_COLUMNS:
        return text
    return defuse_formula(text)


def build_client_csv(companies: Iterable[ClientCompany]) -> str:
    """Render the registry as CSV text, fully materialized.

    Takes an iterable of already-fetched rows rather than a queryset, so that the
    caller is the one holding the transaction open and no lazy evaluation can escape
    into a response iterator.
    """
    buffer = io.StringIO()
    # QUOTE_ALL, not QUOTE_MINIMAL: an unquoted 14-digit legacy CNPJ is what a
    # spreadsheet renders as 1.1222333E+13.
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writerow(EXPORT_COLUMNS)
    for company in companies:
        writer.writerow(
            [_cell(column, getattr(company, column)) for column in EXPORT_COLUMNS],
        )
    return buffer.getvalue()


def parse_client_csv(payload: str) -> list[dict[str, str]]:
    """Read an exported file back, tolerating the byte-order mark the view adds.

    Exists so the round-trip is asserted against a real reader rather than against a
    string comparison that would hide an encoding fault.
    """
    return list(csv.DictReader(io.StringIO(payload.lstrip(UTF8_BOM))))
