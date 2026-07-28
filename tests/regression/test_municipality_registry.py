"""Municipality registry: field shape, platform-level placement, and the unknown case.

The assertion that matters most here is that an **unseeded** municipality yields an
explicit `unknown` rather than an exception or a permissive guess. Brazil has 5,570
municipalities and this registry seeds 27, so the unknown path is the common path, not
the edge case. A lookup that raised would put a `try` block in front of every screen
that displays a client; a lookup that answered with confident defaults would report a
guess in the same shape as a verified fact.

"Conservative" is asserted in both directions, because the two flags point opposite
ways: not claiming the national emitter is conservative, and assuming a certificate IS
required is conservative. A test that only checked "defaults to False" would lock in
the dangerous answer for `requires_certificate`.
"""

import importlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from django.db import IntegrityError, connection, transaction

from apps.core.rls import is_exempt_from_tenant_policy
from apps.fiscal import capabilities
from apps.fiscal import models as models_module
from apps.fiscal.capabilities import (
    UNKNOWN_NFSE_NATIONAL_EMITTER,
    UNKNOWN_REQUIRES_CERTIFICATE,
    CapabilityStatus,
    capability_for,
)
from apps.fiscal.models import (
    IBGE_CODE_LENGTH,
    IBGE_UF_PREFIX_LENGTH,
    UF,
    Municipality,
    MunicipalityCapability,
)

pytestmark = pytest.mark.django_db(transaction=True)

# A migration module name starts with a digit, so it cannot be imported with a plain
# `from ... import`. The seed is reached through the module rather than copied here, so
# the pack and the migration can never disagree about what was seeded.
seed_module = importlib.import_module("apps.fiscal.migrations.0002_seed_capitals")

SAO_PAULO = "3550308"
CURITIBA = "4106902"
UNSEEDED = "3509502"  # Campinas/SP: a real municipality, deliberately not seeded.

# The first two digits of an IBGE municipality code are the state's own IBGE code.
# Listed here so a transcription error in the seed shows up as a mismatch rather than
# as a lookup that silently misses.
UF_IBGE_PREFIX = {
    "RO": "11",
    "AC": "12",
    "AM": "13",
    "RR": "14",
    "PA": "15",
    "AP": "16",
    "TO": "17",
    "MA": "21",
    "PI": "22",
    "CE": "23",
    "RN": "24",
    "PB": "25",
    "PE": "26",
    "AL": "27",
    "SE": "28",
    "BA": "29",
    "MG": "31",
    "ES": "32",
    "RJ": "33",
    "SP": "35",
    "PR": "41",
    "SC": "42",
    "RS": "43",
    "MS": "50",
    "MT": "51",
    "GO": "52",
    "DF": "53",
}


# ================================================================================
# Platform-level placement
# ================================================================================


def test_both_tables_are_allow_listed_as_platform_level() -> None:
    """T-014's inverse check reddens CI if a table is neither scoped nor listed."""
    # Given the two registry tables
    # When they are tested against the allow-list
    # Then both are exempt, because which NFS-e system a city runs is a fact about the
    # city and not about any one accounting firm
    assert is_exempt_from_tenant_policy(Municipality._meta.db_table)
    assert is_exempt_from_tenant_policy(MunicipalityCapability._meta.db_table)


def test_the_table_names_are_the_ones_the_allow_list_actually_spells() -> None:
    """The allow-list holds literal names, so a renamed table silently loses cover."""
    # Given the models
    # When their table names are read
    # Then they match the entries in NON_TENANT_TABLES exactly. Renaming the app or a
    # model without updating the allow-list would leave these tables unaccounted for.
    assert Municipality._meta.db_table == "fiscal_municipality"
    assert MunicipalityCapability._meta.db_table == "fiscal_municipalitycapability"


def test_neither_table_carries_a_tenant_column() -> None:
    # Given the two registry tables
    for model in (Municipality, MunicipalityCapability):
        # When their columns are inspected
        columns = {field.name for field in model._meta.get_fields()}

        # Then neither has a tenant, which is what makes the allow-list entry correct
        # rather than an exemption papering over a missing policy
        assert "tenant" not in columns, model.__name__


def test_neither_table_has_a_row_level_security_policy() -> None:
    # Given the two registry tables
    for model in (Municipality, MunicipalityCapability):
        # When pg_policies is queried
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT policyname FROM pg_policies WHERE schemaname = 'public' "
                "AND tablename = %s",
                [model._meta.db_table],
            )
            policies = cursor.fetchall()

        # Then there is none. A policy keyed on app.tenant_id would make this shared
        # reference data invisible to every tenant under the fail-closed predicate.
        assert policies == [], model.__name__


# ================================================================================
# Field shape — locked, because later waves read these columns
# ================================================================================


def test_the_municipality_field_shape_is_locked() -> None:
    # Given the Municipality model
    fields = {f.name: f for f in Municipality._meta.concrete_fields}

    # When its shape is inspected
    # Then it is IBGE code as primary key, name, and UF
    assert set(fields) == {"ibge_code", "name", "uf"}
    assert fields["ibge_code"].primary_key
    assert fields["ibge_code"].max_length == IBGE_CODE_LENGTH
    assert fields["uf"].max_length == IBGE_UF_PREFIX_LENGTH

    # And the UF column is constrained to the 27 federative units, so a typo in a
    # seed row is rejected rather than creating a 28th "state"
    uf_choices = fields["uf"].choices
    assert uf_choices is not None
    assert {choice[0] for choice in uf_choices} == set(UF.values)


def test_the_capability_field_shape_is_locked() -> None:
    # Given the MunicipalityCapability model
    fields = {f.name: f for f in MunicipalityCapability._meta.concrete_fields}

    # When its shape is inspected
    # Then it carries exactly the columns the plan specifies
    assert {
        "municipality",
        "nfse_national_emitter",
        "requires_certificate",
        "notes",
        "verified_on",
    } <= set(fields)


def test_the_conservative_defaults_are_the_model_defaults_too() -> None:
    """The lookup's unknown answer and the column defaults must not disagree."""
    # Given the capability columns
    fields = {f.name: f for f in MunicipalityCapability._meta.concrete_fields}

    # When their defaults are read
    # Then a row created with no explicit flags is conservative in both directions
    assert fields["nfse_national_emitter"].default is False
    assert fields["requires_certificate"].default is True


def test_one_municipality_cannot_hold_two_contradictory_capability_rows() -> None:
    # Given a municipality that already has a capability row
    municipality = Municipality.objects.get(ibge_code=SAO_PAULO)

    # When a second is inserted for the same city
    # Then the database refuses it, so capability_for can never be non-deterministic
    with pytest.raises(IntegrityError), transaction.atomic():
        MunicipalityCapability.objects.create(municipality=municipality)


# ================================================================================
# The seed
# ================================================================================


def test_all_twenty_seven_capitals_are_seeded() -> None:
    # Given the seeded registry
    # When the capitals are counted
    # Then all 27 are present, one per federative unit
    assert Municipality.objects.count() >= len(seed_module.CAPITALS)
    seeded_ufs = set(
        Municipality.objects.filter(
            ibge_code__in=[code for code, _n, _u in seed_module.CAPITALS],
        ).values_list("uf", flat=True),
    )
    assert seeded_ufs == set(UF.values)


def test_every_seeded_ibge_code_agrees_with_its_state() -> None:
    """A transcription error becomes a permanent lookup miss, so it is checked."""
    # Given every seeded capital
    for ibge_code, name, uf in seed_module.CAPITALS:
        # When the code's first two digits are compared with the state's IBGE code
        # Then they agree, and the code is seven digits
        assert len(ibge_code) == IBGE_CODE_LENGTH, name
        assert ibge_code.isdigit(), name
        assert ibge_code[:IBGE_UF_PREFIX_LENGTH] == UF_IBGE_PREFIX[uf], (
            f"{name}/{uf}: code {ibge_code} does not start with {UF_IBGE_PREFIX[uf]}"
        )


def test_the_seed_is_idempotent() -> None:
    """The suite replays it after truncation, and migrate may be re-run."""
    # Given the already-seeded registry
    before = Municipality.objects.count()

    # When the seed runs again
    seed_module.seed_from_global_registry()

    # Then nothing is duplicated
    assert Municipality.objects.count() == before


def test_the_seed_does_not_claim_capabilities_nobody_verified() -> None:
    """An unverified default dressed up as a verified fact is the failure mode."""
    # Given the seeded capability rows
    seeded = MunicipalityCapability.objects.filter(
        municipality__ibge_code__in=[c for c, _n, _u in seed_module.CAPITALS],
    )

    # When their verification state is inspected
    # Then none claims the national emitter, all assume a certificate is required, and
    # every one is explicitly marked unverified
    assert seeded.exists()
    for capability in seeded:
        assert capability.nfse_national_emitter is False
        assert capability.requires_certificate is True
        assert capability.verified_on is None
        assert "not been verified" in capability.notes


# ================================================================================
# The lookup
# ================================================================================


def test_a_seeded_municipality_returns_a_record() -> None:
    # Given a seeded capital
    # When it is looked up
    answer = capability_for(SAO_PAULO)

    # Then the record identifies the city
    assert answer.ibge_code == SAO_PAULO
    assert answer.name == "São Paulo"
    assert answer.uf == "SP"


def test_a_verified_municipality_reports_known() -> None:
    # Given a municipality whose capabilities a human has actually assessed
    municipality = Municipality.objects.get(ibge_code=CURITIBA)
    capability = municipality.capability
    capability.nfse_national_emitter = True
    capability.requires_certificate = False
    capability.notes = "Checked against the municipal portal."
    capability.verified_on = datetime.now(tz=UTC).date()
    capability.save()

    # When it is looked up
    answer = capability_for(CURITIBA)

    # Then the answer is reported as known, and carries the verified values
    assert answer.status == CapabilityStatus.KNOWN
    assert answer.is_known is True
    assert answer.nfse_national_emitter is True
    assert answer.requires_certificate is False
    assert answer.verified_on == datetime.now(tz=UTC).date()


@pytest.mark.parametrize(
    "code",
    [UNSEEDED, "9999999", "", "   ", "not-a-code", "0000000"],
)
def test_an_unseeded_code_returns_unknown_and_never_raises(code: str) -> None:
    """5,543 of Brazil's municipalities are unseeded, so this IS the common path."""
    # Given a code that is not in the registry, malformed, or blank
    # When it is looked up
    answer = capability_for(code)

    # Then it answers unknown rather than raising
    assert answer.status == CapabilityStatus.UNKNOWN
    assert answer.is_known is False


def test_an_unseeded_code_defaults_conservatively_in_both_directions() -> None:
    """The T-035 failure criterion. Both flags, because they point opposite ways."""
    # Given a municipality nobody has assessed
    # When it is looked up
    answer = capability_for(UNSEEDED)

    # Then the national emitter is NOT claimed. Claiming it would send a firm down an
    # integration path that may not exist for that city — the "silent True" forbidden
    # by the acceptance criteria.
    assert answer.nfse_national_emitter is False
    assert answer.nfse_national_emitter == UNKNOWN_NFSE_NATIONAL_EMITTER

    # And a certificate IS assumed to be required. This is the conservative direction:
    # wrongly assuming one costs an unnecessary purchase, while wrongly assuming none
    # tells a client they can issue invoices with a gov.br login and they find out
    # otherwise on the day of a deadline.
    assert answer.requires_certificate is True
    assert answer.requires_certificate == UNKNOWN_REQUIRES_CERTIFICATE


def test_the_two_conservative_defaults_point_in_opposite_directions() -> None:
    """Guards against a future "tidy-up" that makes both default to False."""
    # Given the documented unknown-case policy
    # When the two constants are compared
    # Then they differ. "Conservative" means assume the least convenient truth, not
    # assume False, and collapsing them would silently make the certificate answer
    # permissive.
    assert UNKNOWN_NFSE_NATIONAL_EMITTER is False
    assert UNKNOWN_REQUIRES_CERTIFICATE is True
    assert UNKNOWN_NFSE_NATIONAL_EMITTER != UNKNOWN_REQUIRES_CERTIFICATE


def test_a_registered_municipality_with_no_assessment_is_still_unknown() -> None:
    """Being in the registry and being assessed are different facts."""
    # Given a municipality row with no capability row attached
    Municipality.objects.create(ibge_code="3509502", name="Campinas", uf="SP")

    # When it is looked up
    answer = capability_for("3509502")

    # Then it is unknown, but the city is still identified, and the flags are
    # conservative. Reporting this as known-with-defaults would be exactly the
    # conflation the registry exists to prevent.
    assert answer.status == CapabilityStatus.UNKNOWN
    assert answer.name == "Campinas"
    assert answer.uf == "SP"
    assert answer.nfse_national_emitter is False
    assert answer.requires_certificate is True


def test_the_unknown_answer_says_so_in_words_a_reader_will_see() -> None:
    # Given an unseeded municipality
    # When it is looked up
    answer = capability_for(UNSEEDED)

    # Then the note says the values are defaults rather than facts, so the state
    # reaches a human reading a screen and not only a branch in code
    assert "conservative defaults" in answer.notes
    assert "not verified facts" in answer.notes


def test_the_registry_reaches_no_network() -> None:
    """Scope limit: T-035 is a data-shape registry. NFS-e API calls are Phase 3."""
    # Given the modules that implement the registry
    sources = [
        Path(capabilities.__file__ or ""),
        Path(models_module.__file__ or ""),
    ]
    assert all(path.is_file() for path in sources)

    # When their source is read
    text = "\n".join(path.read_text(encoding="utf-8") for path in sources)

    # Then none of them imports anything that could reach a municipal NFS-e endpoint.
    # Building a connector here would put network calls behind a lookup that every
    # client screen performs, four waves before the connector work is planned.
    forbidden_imports = (
        "import requests",
        "import urllib",
        "import httpx",
        "import socket",
    )
    for forbidden in forbidden_imports:
        assert forbidden not in text, f"{forbidden!r} appears in the registry modules"
