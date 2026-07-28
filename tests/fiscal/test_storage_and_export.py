"""Storage, masks, search and export for alphanumeric-ready documents.

The export assertions are the load-bearing ones. A CSV of CNPJs is the single easiest
artefact in this product to get silently wrong: a spreadsheet turns `00000000000191`
into `1.91E+11`, drops the leading zeros, and the file still opens, still looks like a
client list, and is wrong in a way nobody notices until a filing is rejected.

The response type is asserted too. `TenantMiddleware` raises `TypeError` on a streaming
response, because a streaming iterator runs after the tenant transaction has closed and
every lazy query inside it returns zero rows — a file that downloads successfully and
contains nothing. That is a decision from T-011 and this suite is where a regression
would show up.
"""

from http import HTTPStatus

import pytest
from django.core.exceptions import ValidationError
from django.http import HttpResponse, StreamingHttpResponse
from django.urls import reverse

from apps.accounts.models import User
from apps.clients.exports import (
    DOCUMENT_COLUMNS,
    EXPORT_COLUMNS,
    FORMULA_PREFIXES,
    UTF8_BOM,
    build_client_csv,
    defuse_formula,
    parse_client_csv,
)
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.fiscal.formatting import format_cnpj, format_cpf
from apps.fiscal.validators import CNPJ_ALPHABET
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.support import enrol_totp, sign_in

pytestmark = pytest.mark.django_db(transaction=True)

# The official RFB vector, and a legacy numeric CNPJ whose leading zeros are exactly
# what a spreadsheet destroys.
ALPHANUMERIC_CNPJ = "12ABC34501DE35"
MASKED_ALPHANUMERIC = "12.ABC.345/01DE-35"
LEADING_ZERO_CNPJ = "00000000000191"
LEGACY_CNPJ = "11222333000181"
VALID_CPF = "11144477735"

PASSWORD = "correct-horse-battery-staple"  # noqa: S105


class Firm:
    """One firm with an owner who may export, and a two-client registry."""

    def __init__(self, slug: str = "alfa") -> None:
        self.tenant = Tenant.objects.create(name=slug.title(), slug=slug)
        self.owner = User.objects.create_user(
            email=f"owner@{slug}.example.com",
            password=PASSWORD,
        )
        Membership.objects.create(
            user=self.owner,
            tenant=self.tenant,
            role=TenantRole.OWNER,
        )
        enrol_totp(self.owner)

    def add(self, cnpj: str, legal_name: str, **extra: object) -> ClientCompany:
        with tenant_context(self.tenant.id):
            return ClientCompany.objects.create(
                tenant=self.tenant,
                legal_name=legal_name,
                cnpj=cnpj,
                **extra,
            )


@pytest.fixture
def firm() -> Firm:
    return Firm()


# --------------------------------------------------------------------------------
# Display masks
# --------------------------------------------------------------------------------


def test_the_display_mask_matches_the_official_format() -> None:
    # Given the CNPJ Receita Federal publishes its worked example for
    # When it is formatted
    # Then it matches the official mask exactly, character for character
    assert format_cnpj(ALPHANUMERIC_CNPJ) == MASKED_ALPHANUMERIC


def test_the_display_mask_works_for_legacy_numeric_cnpjs_too() -> None:
    # Given a legacy all-numeric CNPJ
    # When it is formatted
    # Then it uses the same grouping — the mask did not change, only the alphabet
    assert format_cnpj(LEGACY_CNPJ) == "11.222.333/0001-81"
    assert format_cnpj(LEADING_ZERO_CNPJ) == "00.000.000/0001-91"


def test_formatting_is_idempotent_through_an_already_masked_value() -> None:
    # Given a value that already carries its mask
    # When it is formatted again
    # Then the result is unchanged, because the helper normalizes before masking
    assert format_cnpj(MASKED_ALPHANUMERIC) == MASKED_ALPHANUMERIC
    assert format_cnpj(ALPHANUMERIC_CNPJ.lower()) == MASKED_ALPHANUMERIC


def test_the_cpf_mask_matches_the_official_format() -> None:
    # Given a valid CPF
    # When it is formatted
    # Then it matches AAA.AAA.AAA-DD
    assert format_cpf(VALID_CPF) == "111.444.777-35"


def test_masking_a_wrong_length_value_is_refused_rather_than_silently_padded() -> None:
    # Given a value that is not a whole document
    # When it is formatted
    # Then it raises, because a half-masked document on a screen reads as a real one
    with pytest.raises(ValueError, match="cannot be masked"):
        format_cnpj("12ABC")


# --------------------------------------------------------------------------------
# Normalized storage
# --------------------------------------------------------------------------------


def test_a_masked_cnpj_is_stored_normalized(firm: Firm) -> None:
    # Given a client created from a masked, lowercase CNPJ
    company = firm.add("12.abc.345/01de-35", "Padaria do Zé ME")

    # When it is read back from the database, inside the tenant context the
    # row-level-security policy requires
    with tenant_context(firm.tenant.id):
        company.refresh_from_db()

    # Then the stored value carries no punctuation and is uppercase
    assert company.cnpj == ALPHANUMERIC_CNPJ


def test_normalization_happens_on_save_not_only_through_forms(firm: Firm) -> None:
    """`clean` runs for ModelForms and nothing else, so `save` must normalize too."""
    # Given a client written straight through the manager, with no full_clean call
    company = firm.add(MASKED_ALPHANUMERIC, "Direto ME")

    # When the row is re-read
    with tenant_context(firm.tenant.id):
        company.refresh_from_db()

    # Then it is normalized. Without this an import or a management command would
    # store a value that no search could ever match again.
    assert company.cnpj == ALPHANUMERIC_CNPJ


def test_full_clean_accepts_a_masked_cnpj(firm: Firm) -> None:
    """Normalizing in `clean` is what lets the CHECK constraint see a clean value."""
    # Given an unsaved client holding a masked CNPJ
    with tenant_context(firm.tenant.id):
        company = ClientCompany(
            tenant=firm.tenant,
            legal_name="Validada ME",
            cnpj=MASKED_ALPHANUMERIC,
            cpf="111.444.777-35",
        )

        # When it is fully validated
        company.full_clean()

        # Then validation passes and the instance is left normalized, so the database
        # CHECK on ^[0-9A-Z]{14}$ is satisfied by the value that actually gets written
        assert company.cnpj == ALPHANUMERIC_CNPJ
        assert company.cpf == VALID_CPF
        company.save()


def test_full_clean_rejects_a_cnpj_with_bad_check_digits(firm: Firm) -> None:
    # Given a client whose CNPJ has a corrupted final digit
    with tenant_context(firm.tenant.id):
        company = ClientCompany(
            tenant=firm.tenant,
            legal_name="Errada ME",
            cnpj="12ABC34501DE34",
        )

        # When it is validated
        with pytest.raises(ValidationError) as caught:
            company.full_clean()

    # Then the validator wired onto the field is what refuses it
    assert "cnpj" in caught.value.error_dict


def test_the_leading_zero_cnpj_survives_a_round_trip_to_the_database(
    firm: Firm,
) -> None:
    # Given a CNPJ whose first four characters are zeros
    company = firm.add(LEADING_ZERO_CNPJ, "Banco Exemplo S.A.")

    # When it is read back
    with tenant_context(firm.tenant.id):
        company.refresh_from_db()

    # Then every leading zero is still there
    assert company.cnpj == LEADING_ZERO_CNPJ
    assert company.cnpj.startswith("0000")


# --------------------------------------------------------------------------------
# Search by mask
# --------------------------------------------------------------------------------


def test_searching_by_a_masked_cnpj_finds_a_record_stored_normalized(
    firm: Firm,
) -> None:
    """The headline acceptance criterion: paste from an email, find the client."""
    # Given a client stored normalized
    company = firm.add(ALPHANUMERIC_CNPJ, "Mascarada ME")

    # When the user searches using the masked spelling
    with tenant_context(firm.tenant.id):
        found = list(ClientCompany.objects.search(MASKED_ALPHANUMERIC))

    # Then the record is found
    assert [c.pk for c in found] == [company.pk]


@pytest.mark.parametrize(
    "query",
    [
        "12.ABC.345/01DE-35",
        "12ABC34501DE35",
        "12abc34501de35",
        "  12.ABC.345/01DE-35  ",
        "12ABC345",
        "Mascarada",
    ],
)
def test_every_spelling_of_the_query_finds_the_same_client(
    firm: Firm,
    query: str,
) -> None:
    # Given a client stored normalized
    company = firm.add(ALPHANUMERIC_CNPJ, "Mascarada ME")

    # When it is searched for by mask, bare, lowercase, padded, partial, or by name
    with tenant_context(firm.tenant.id):
        found = list(ClientCompany.objects.search(query))

    # Then all of them reach the same row
    assert [c.pk for c in found] == [company.pk]


def test_a_punctuation_only_search_does_not_return_the_whole_registry(
    firm: Firm,
) -> None:
    """A normalized query of "" would make `cnpj__contains` match everything."""
    # Given a registry with clients in it
    firm.add(ALPHANUMERIC_CNPJ, "Uma ME")
    firm.add(LEGACY_CNPJ, "Outra ME")

    # When a user searches for punctuation alone, which normalizes to nothing
    with tenant_context(firm.tenant.id):
        found = list(ClientCompany.objects.search("./-"))

    # Then the registry is not accidentally exported through the search box
    assert found == []


def test_an_empty_search_returns_nothing_rather_than_everything(firm: Firm) -> None:
    # Given a registry with a client in it
    firm.add(ALPHANUMERIC_CNPJ, "Uma ME")

    # When the search term is blank
    with tenant_context(firm.tenant.id):
        found = list(ClientCompany.objects.search("   "))

    # Then nothing is returned
    assert found == []


def test_search_stays_inside_the_tenant(firm: Firm) -> None:
    # Given two firms that both serve the same MEI, which is legitimate
    other = Firm(slug="beta")
    firm.add(ALPHANUMERIC_CNPJ, "Alfa's copy")
    other.add(ALPHANUMERIC_CNPJ, "Beta's copy")

    # When one firm searches for the shared CNPJ
    with tenant_context(firm.tenant.id):
        found = list(ClientCompany.objects.search(MASKED_ALPHANUMERIC))

    # Then it sees only its own record
    assert [c.legal_name for c in found] == ["Alfa's copy"]


# --------------------------------------------------------------------------------
# CSV export
# --------------------------------------------------------------------------------


def test_the_export_round_trips_an_alphanumeric_cnpj_byte_for_byte(
    firm: Firm,
) -> None:
    # Given a registry holding alphanumeric and legacy numeric clients
    firm.add(ALPHANUMERIC_CNPJ, "Alfanumérica ME", cpf=VALID_CPF)
    firm.add(LEGACY_CNPJ, "Legada ME")
    firm.add(LEADING_ZERO_CNPJ, "Zeros à Esquerda S.A.")

    # When the registry is exported and read back
    with tenant_context(firm.tenant.id):
        payload = build_client_csv(list(ClientCompany.objects.all()))
    rows = parse_client_csv(payload)

    # Then every document comes back exactly as it was stored
    exported = {row["legal_name"]: row["cnpj"] for row in rows}
    assert exported["Alfanumérica ME"] == ALPHANUMERIC_CNPJ
    assert exported["Legada ME"] == LEGACY_CNPJ
    assert exported["Zeros à Esquerda S.A."] == LEADING_ZERO_CNPJ
    assert rows[0]["cpf"] in {VALID_CPF, ""}


def test_the_export_never_produces_scientific_notation_or_drops_a_zero(
    firm: Firm,
) -> None:
    """The T-033 failure criterion, asserted against the file's actual bytes."""
    # Given the CNPJs most likely to be coerced into numbers
    firm.add(LEADING_ZERO_CNPJ, "Zeros S.A.")
    firm.add(LEGACY_CNPJ, "Legada ME")

    # When the file is produced
    with tenant_context(firm.tenant.id):
        payload = build_client_csv(list(ClientCompany.objects.all()))

    # Then the literal characters are present in the file
    assert f'"{LEADING_ZERO_CNPJ}"' in payload
    assert f'"{LEGACY_CNPJ}"' in payload

    # And nothing resembling scientific notation appears anywhere
    assert "E+" not in payload.upper()

    # And the truncated forms a coercing writer would emit are absent
    assert '"191"' not in payload
    assert '"1.91' not in payload


def test_every_field_is_quoted_because_quoting_is_what_defeats_coercion(
    firm: Firm,
) -> None:
    # Given an exported registry
    firm.add(LEADING_ZERO_CNPJ, "Zeros S.A.")
    with tenant_context(firm.tenant.id):
        payload = build_client_csv(list(ClientCompany.objects.all()))

    # When the header line is inspected
    header = payload.splitlines()[0]

    # Then every column, header included, is quoted. QUOTE_MINIMAL would leave a bare
    # 14-digit CNPJ for the spreadsheet to reinterpret as a number.
    assert header == ",".join(f'"{column}"' for column in EXPORT_COLUMNS)


def test_a_document_column_is_never_rewritten_by_the_injection_guard() -> None:
    """Defusing must not touch documents, or the round-trip would not be exact."""
    # Given the full document alphabet
    # When each character is tested against the formula prefixes
    # Then none of them collides, so a document can never begin with one and the
    # guard can never alter it. This is what makes byte-for-byte round-trip safe.
    assert not set(CNPJ_ALPHABET) & set(FORMULA_PREFIXES)
    assert set(EXPORT_COLUMNS) >= DOCUMENT_COLUMNS
    for char in CNPJ_ALPHABET:
        assert defuse_formula(char * 14) == char * 14


def test_a_client_named_like_a_formula_is_defused(firm: Firm) -> None:
    """Excel executes a cell starting with '='. A client name is attacker-influenced."""
    # Given a client whose legal name is a spreadsheet formula
    firm.add(ALPHANUMERIC_CNPJ, "=cmd|'/c calc'!A1")

    # When the registry is exported
    with tenant_context(firm.tenant.id):
        payload = build_client_csv(list(ClientCompany.objects.all()))
    rows = parse_client_csv(payload)

    # Then the name is neutralized with a leading apostrophe
    assert rows[0]["legal_name"].startswith("'=")

    # And the CNPJ in the same row is untouched
    assert rows[0]["cnpj"] == ALPHANUMERIC_CNPJ


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@"])
def test_each_formula_prefix_is_defused(prefix: str) -> None:
    # Given a value beginning with a character a spreadsheet executes
    # When it is defused
    # Then it is prefixed with an apostrophe
    assert defuse_formula(f"{prefix}SUM(A1)") == f"'{prefix}SUM(A1)"


def test_an_ordinary_value_is_not_defused() -> None:
    # Given an ordinary legal name
    # When it is defused
    # Then it is returned unchanged, so the file stays readable
    assert defuse_formula("Padaria do Zé ME") == "Padaria do Zé ME"


# --------------------------------------------------------------------------------
# The export VIEW: response type, and the streaming prohibition
# --------------------------------------------------------------------------------


def test_the_export_view_returns_a_materialized_response_not_a_streaming_one(
    firm: Firm,
) -> None:
    """A StreamingHttpResponse here would download successfully and be empty."""
    # Given a signed-in owner and a client on the books
    firm.add(ALPHANUMERIC_CNPJ, "Alfanumérica ME")
    client = sign_in(firm.owner, PASSWORD, with_mfa=True)

    # When the export is requested
    response = client.get(
        reverse("clients-export-csv"),
        headers={"host": f"{firm.tenant.slug}.localhost"},
    )

    # Then it is a plain, fully materialized response
    assert response.status_code == HTTPStatus.OK
    assert isinstance(response, HttpResponse)
    assert not isinstance(response, StreamingHttpResponse)
    assert response.streaming is False

    # And it is served as a downloadable attachment
    assert response["Content-Disposition"].startswith("attachment;")
    assert "text/csv" in response["Content-Type"]


def test_the_exported_file_actually_contains_the_firms_clients(firm: Firm) -> None:
    """The failure mode a streaming response would produce is an EMPTY file."""
    # Given three clients on the books
    firm.add(ALPHANUMERIC_CNPJ, "Alfanumérica ME")
    firm.add(LEGACY_CNPJ, "Legada ME")
    firm.add(LEADING_ZERO_CNPJ, "Zeros S.A.")
    client = sign_in(firm.owner, PASSWORD, with_mfa=True)

    # When the export is downloaded
    response = client.get(
        reverse("clients-export-csv"),
        headers={"host": f"{firm.tenant.slug}.localhost"},
    )
    body = response.content.decode("utf-8-sig")
    rows = parse_client_csv(body)

    # Then all three rows are present. Under a streaming response every query inside
    # the iterator would have run outside the tenant transaction and returned nothing,
    # and this assertion is what would catch that.
    assert len(rows) == 3
    assert {row["cnpj"] for row in rows} == {
        ALPHANUMERIC_CNPJ,
        LEGACY_CNPJ,
        LEADING_ZERO_CNPJ,
    }


def test_the_downloaded_file_carries_a_byte_order_mark_for_accented_names(
    firm: Firm,
) -> None:
    # Given a client whose legal name is accented, as most Brazilian names are
    firm.add(ALPHANUMERIC_CNPJ, "Padaria do Zé ME")
    client = sign_in(firm.owner, PASSWORD, with_mfa=True)

    # When the file is downloaded
    response = client.get(
        reverse("clients-export-csv"),
        headers={"host": f"{firm.tenant.slug}.localhost"},
    )

    # Then it opens with a UTF-8 byte-order mark, without which a spreadsheet reads
    # the file as the system codepage and renders "Zé" as mojibake
    assert response.content.startswith(UTF8_BOM.encode("utf-8"))

    # And the name still parses back intact
    rows = parse_client_csv(response.content.decode("utf-8-sig"))
    assert rows[0]["legal_name"] == "Padaria do Zé ME"


def test_the_export_does_not_leak_another_firms_clients(firm: Firm) -> None:
    # Given two firms that each hold the same MEI
    other = Firm(slug="beta")
    firm.add(ALPHANUMERIC_CNPJ, "Alfa's copy")
    other.add(LEGACY_CNPJ, "Beta only")
    client = sign_in(firm.owner, PASSWORD, with_mfa=True)

    # When one firm exports
    response = client.get(
        reverse("clients-export-csv"),
        headers={"host": f"{firm.tenant.slug}.localhost"},
    )
    rows = parse_client_csv(response.content.decode("utf-8-sig"))

    # Then the other firm's client is absent
    assert {row["legal_name"] for row in rows} == {"Alfa's copy"}
