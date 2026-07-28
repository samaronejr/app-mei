"""Day-one regression pack: an alphanumeric CNPJ survives every layer, end to end.

Alphanumeric CNPJs entered production for new registrations on 2026-07-31 under
IN RFB nº 2.229/2024. Legacy all-numeric CNPJs remain valid and unchanged, so the two
formats coexist and **both** are driven through the same pass here. A pack that
exercised only the new format would not notice a change that broke the old one, and
the overwhelming majority of this product's rows are still legacy numbers.

The pack walks one client through seven layers in order:

    validation -> normalization -> model save -> queryset filter
               -> admin search -> CSV export -> re-import

Each layer is also asserted on its own, so that deleting the normalizer from any one
of them turns a *named* test red rather than producing a vague end-to-end failure that
takes an afternoon to localize. `.evidence/T-034-failure.txt` records that being done
deliberately to the search layer and the pack catching it there specifically.

CI runs this file as its own step. A regression here is a correctness failure in the
product's most fundamental identifier, and it must not arrive as one line inside a
five-hundred-test summary.
"""

from http import HTTPStatus

import pytest
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory
from django.urls import reverse

from apps.accounts.models import User
from apps.clients.admin import ClientCompanyAdmin
from apps.clients.exports import build_client_csv, parse_client_csv
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.fiscal.formatting import format_cnpj
from apps.fiscal.validators import normalize_document, validate_cnpj
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.support import enrol_totp, sign_in

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = "correct-horse-battery-staple"  # noqa: S105


class Format:
    """One CNPJ in every spelling this product has to accept."""

    def __init__(self, label: str, stored: str, masked: str, name: str) -> None:
        self.label = label
        self.stored = stored
        self.masked = masked
        self.name = name

    def __repr__(self) -> str:
        return self.label


# The official RFB vector, and a real legacy CNPJ whose leading zeros are the thing a
# spreadsheet destroys. Both must pass every layer.
ALPHANUMERIC = Format(
    "alphanumeric",
    "12ABC34501DE35",
    "12.ABC.345/01DE-35",
    "Alfanumérica ME",
)
LEGACY_NUMERIC = Format(
    "legacy-numeric",
    "11222333000181",
    "11.222.333/0001-81",
    "Legada ME",
)
LEADING_ZEROS = Format(
    "leading-zeros",
    "00000000000191",
    "00.000.000/0001-91",
    "Zeros à Esquerda S.A.",
)

BOTH_FORMATS = pytest.mark.parametrize(
    "fmt",
    [ALPHANUMERIC, LEGACY_NUMERIC, LEADING_ZEROS],
    ids=lambda f: f.label,
)


class Firm:
    """A firm whose owner may export, so the HTTP layer can be exercised too."""

    def __init__(self, slug: str = "pack") -> None:
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

    def add(self, cnpj: str, legal_name: str) -> ClientCompany:
        with tenant_context(self.tenant.id):
            return ClientCompany.objects.create(
                tenant=self.tenant,
                legal_name=legal_name,
                cnpj=cnpj,
            )


@pytest.fixture
def firm() -> Firm:
    return Firm()


def _admin_search(user: User, term: str) -> list[ClientCompany]:
    """Drive the real admin search path, not a reimplementation of it."""
    admin = ClientCompanyAdmin(ClientCompany, AdminSite())
    request = RequestFactory().get("/admin/clients/clientcompany/", {"q": term})
    request.user = user
    queryset, _ = admin.get_search_results(
        request,
        ClientCompany.objects.all(),
        term,
    )
    return list(queryset)


# ================================================================================
# THE END-TO-END PASS
# ================================================================================


@BOTH_FORMATS
def test_a_cnpj_survives_every_layer_in_one_pass(firm: Firm, fmt: Format) -> None:
    """validation -> normalization -> save -> filter -> admin -> export -> re-import."""
    # LAYER 1 -- validation accepts the masked spelling a user actually pastes
    validate_cnpj(fmt.masked)

    # LAYER 2 -- normalization reduces every spelling to one stored form
    assert normalize_document(fmt.masked) == fmt.stored
    assert normalize_document(fmt.masked.lower()) == fmt.stored

    # LAYER 3 -- the model stores it normalized even though it arrived masked
    company = firm.add(fmt.masked, fmt.name)
    with tenant_context(firm.tenant.id):
        company.refresh_from_db()
    assert company.cnpj == fmt.stored

    # LAYER 4 -- a queryset filter finds it from the masked spelling
    with tenant_context(firm.tenant.id):
        by_mask = list(ClientCompany.objects.search(fmt.masked))
    assert [c.pk for c in by_mask] == [company.pk]

    # LAYER 5 -- the admin search finds it from the masked spelling too
    with tenant_context(firm.tenant.id):
        by_admin = _admin_search(firm.owner, fmt.masked)
    assert [c.pk for c in by_admin] == [company.pk]

    # LAYER 6 -- the export emits the normalized value, unmangled
    with tenant_context(firm.tenant.id):
        payload = build_client_csv(list(ClientCompany.objects.all()))
    assert f'"{fmt.stored}"' in payload

    # LAYER 7 -- re-import returns exactly what was stored, byte for byte
    rows = parse_client_csv(payload)
    assert [row["cnpj"] for row in rows] == [fmt.stored]

    # And the display mask is the official one at the end of all that
    assert format_cnpj(rows[0]["cnpj"]) == fmt.masked


def test_both_formats_coexist_in_one_registry(firm: Firm) -> None:
    """IN RFB 2.229/2024 is backward compatible: neither format displaces the other."""
    # Given a firm holding an alphanumeric and two legacy numeric clients
    for fmt in (ALPHANUMERIC, LEGACY_NUMERIC, LEADING_ZEROS):
        firm.add(fmt.masked, fmt.name)

    # When the registry is exported and read back
    with tenant_context(firm.tenant.id):
        payload = build_client_csv(list(ClientCompany.objects.all()))
    rows = parse_client_csv(payload)

    # Then all three are present, in both formats, each exactly as stored
    assert {row["cnpj"] for row in rows} == {
        ALPHANUMERIC.stored,
        LEGACY_NUMERIC.stored,
        LEADING_ZEROS.stored,
    }

    # And each is still individually findable by its own mask
    for fmt in (ALPHANUMERIC, LEGACY_NUMERIC, LEADING_ZEROS):
        with tenant_context(firm.tenant.id):
            found = list(ClientCompany.objects.search(fmt.masked))
        assert [c.cnpj for c in found] == [fmt.stored], fmt.label


# ================================================================================
# PER-LAYER ASSERTIONS -- so a break is localized to the layer that caused it
# ================================================================================


@BOTH_FORMATS
def test_layer_validation_accepts_every_spelling(fmt: Format) -> None:
    # Given a CNPJ written masked, bare, lowercase and padded
    # When each is validated
    # Then all are accepted
    for spelling in (
        fmt.masked,
        fmt.stored,
        fmt.masked.lower(),
        f"  {fmt.masked}  ",
    ):
        validate_cnpj(spelling)


@BOTH_FORMATS
def test_layer_normalization_is_the_single_definition_of_stored_form(
    fmt: Format,
) -> None:
    # Given every spelling of one CNPJ
    spellings = [fmt.masked, fmt.stored, fmt.masked.lower(), f" {fmt.stored} "]

    # When each is normalized
    # Then they all collapse to exactly one value
    assert {normalize_document(s) for s in spellings} == {fmt.stored}


@BOTH_FORMATS
def test_layer_model_save_stores_normalized(firm: Firm, fmt: Format) -> None:
    # Given a client created straight through the manager from a masked CNPJ
    company = firm.add(fmt.masked, fmt.name)

    # When the row is re-read from the database
    with tenant_context(firm.tenant.id):
        company.refresh_from_db()

    # Then the column holds the bare characters
    assert company.cnpj == fmt.stored


@BOTH_FORMATS
def test_layer_queryset_filter_matches_a_masked_query(
    firm: Firm,
    fmt: Format,
) -> None:
    # Given a client stored normalized
    company = firm.add(fmt.stored, fmt.name)

    # When the queryset is filtered by the masked spelling
    with tenant_context(firm.tenant.id):
        found = list(ClientCompany.objects.search(fmt.masked))

    # Then the record is found. Removing the normalizer here makes THIS test red.
    assert [c.pk for c in found] == [company.pk]


@BOTH_FORMATS
def test_layer_admin_search_matches_a_masked_query(
    firm: Firm,
    fmt: Format,
) -> None:
    # Given a client stored normalized
    company = firm.add(fmt.stored, fmt.name)

    # When an operator pastes the masked CNPJ into the admin search box
    with tenant_context(firm.tenant.id):
        found = _admin_search(firm.owner, fmt.masked)

    # Then the record is found. Removing the normalizer here makes THIS test red.
    assert [c.pk for c in found] == [company.pk]


@BOTH_FORMATS
def test_layer_export_emits_the_normalized_value_unmangled(
    firm: Firm,
    fmt: Format,
) -> None:
    # Given a client on the books
    firm.add(fmt.stored, fmt.name)

    # When the registry is exported
    with tenant_context(firm.tenant.id):
        payload = build_client_csv(list(ClientCompany.objects.all()))

    # Then the value appears quoted and intact, with no coercion artefacts
    assert f'"{fmt.stored}"' in payload
    assert "E+" not in payload.upper()

    # And where the CNPJ has leading zeros, the form a coercing writer would have
    # emitted instead is absent — "00000000000191" must never appear as "191"
    truncated = fmt.stored.lstrip("0")
    if truncated != fmt.stored:
        assert f'"{truncated}"' not in payload


@BOTH_FORMATS
def test_layer_reimport_round_trips_byte_for_byte(firm: Firm, fmt: Format) -> None:
    # Given an exported registry
    firm.add(fmt.stored, fmt.name)
    with tenant_context(firm.tenant.id):
        payload = build_client_csv(list(ClientCompany.objects.all()))

    # When the file is read back and the value is fed to the model again
    rows = parse_client_csv(payload)
    reimported = rows[0]["cnpj"]

    # Then it is identical to what was stored, and it still validates
    assert reimported == fmt.stored
    validate_cnpj(reimported)

    # And re-importing it into a second firm produces the same stored value, which is
    # what an inter-system transfer actually depends on
    other = Firm(slug="reimport")
    copy = other.add(reimported, fmt.name)
    with tenant_context(other.tenant.id):
        copy.refresh_from_db()
    assert copy.cnpj == fmt.stored


@BOTH_FORMATS
def test_layer_http_download_delivers_the_document_intact(
    firm: Firm,
    fmt: Format,
) -> None:
    """The whole stack, through a real request, not just the helper functions."""
    # Given a signed-in owner and a client on the books
    firm.add(fmt.masked, fmt.name)
    client = sign_in(firm.owner, PASSWORD, with_mfa=True)

    # When the CSV is downloaded over HTTP
    response = client.get(
        reverse("clients-export-csv"),
        headers={"host": f"{firm.tenant.slug}.localhost"},
    )

    # Then the download succeeds and is not a streaming response, whose iterator
    # would have run outside the tenant transaction and produced an empty file
    assert response.status_code == HTTPStatus.OK
    assert response.streaming is False

    # And the document arrives exactly as stored
    rows = parse_client_csv(response.content.decode("utf-8-sig"))
    assert [row["cnpj"] for row in rows] == [fmt.stored]


def test_the_pack_covers_both_formats_and_is_not_vacuous() -> None:
    """Guards the pack itself: a trimmed corpus would make the sweep meaningless."""
    # Given the corpus this pack parametrizes over
    corpus = [ALPHANUMERIC, LEGACY_NUMERIC, LEADING_ZEROS]

    # When it is inspected
    # Then it contains at least one alphanumeric and at least one legacy numeric CNPJ
    assert any(not f.stored.isdigit() for f in corpus), "no alphanumeric fixture"
    assert any(f.stored.isdigit() for f in corpus), "no legacy numeric fixture"
    assert any(f.stored.startswith("0") for f in corpus), "no leading-zero fixture"

    # And every fixture's stored form really is the normalization of its mask
    for fmt in corpus:
        assert normalize_document(fmt.masked) == fmt.stored
        assert format_cnpj(fmt.stored) == fmt.masked
