"""The readiness score, the e-CAC blocker, and the field that must never exist.

`test_no_certificate_material_can_be_stored_anywhere` is the important one and it is
deliberately an introspection test rather than a behavioural one. Custody of a client's
A1 certificate would make this product a holder of signing keys for every firm on it,
which the anchor forbids in v1. A behavioural test could only prove that today's code
does not write one; introspection proves the column to write it into does not exist.
"""

import pytest
from django.apps import apps as django_apps
from django.db import models

from apps.clients.models import (
    ClientCompany,
    GovBrTrustLevel,
    OnboardingItemTemplate,
    OnboardingStatus,
)
from apps.clients.readiness import ChecklistEntry, ecac_blocker, score
from apps.core.tenancy import tenant_context
from apps.tenants.models import Tenant
from tests.isolation.rolecheck import assert_isolated_role

pytestmark = pytest.mark.django_db

DEFAULT_ITEM_COUNT = 6
ECAC_ITEM_COUNT = 2
FORBIDDEN_FIELDS = (models.FileField, models.BinaryField, models.ImageField)


@pytest.fixture
def tenant() -> Tenant:
    return Tenant.objects.create(name="Alpha", slug="alpha")


def _client(tenant: Tenant, **overrides: object) -> ClientCompany:
    with tenant_context(tenant.id):
        return ClientCompany.objects.create(
            tenant=tenant,
            legal_name="Padaria do Ze MEI",
            cnpj="AAAAAAAAAAAA01",
            **overrides,
        )


def _mark(client: ClientCompany, keys: list[str], status: str) -> None:
    client.onboarding_items.filter(key__in=keys).update(status=status)


def test_a_fresh_client_scores_zero(tenant: Tenant) -> None:
    # Given a newly registered client, every checklist item pending
    assert_isolated_role()
    client = _client(tenant)

    # When readiness is computed
    # Then it is 0
    with tenant_context(tenant.id):
        assert client.readiness_score == 0
        assert client.is_ready is False


def test_settling_every_item_scores_one_hundred(tenant: Tenant) -> None:
    # Given a client whose whole checklist is done
    assert_isolated_role()
    client = _client(tenant, govbr_trust_level=GovBrTrustLevel.OURO)

    with tenant_context(tenant.id):
        client.onboarding_items.update(status=OnboardingStatus.DONE)

        # When readiness is computed
        # Then it is 100 and nothing is blocking
        assert client.readiness_score == 100
        assert client.is_ready is True
        assert client.ecac_blocker is None


def test_the_score_climbs_with_each_item_settled(tenant: Tenant) -> None:
    # Given a fresh client with six items
    assert_isolated_role()
    client = _client(tenant, govbr_trust_level=GovBrTrustLevel.PRATA)

    with tenant_context(tenant.id):
        keys = list(client.onboarding_items.values_list("key", flat=True))
        progression = []

        # When items are settled one at a time
        for index, key in enumerate(keys, start=1):
            _mark(client, [key], OnboardingStatus.DONE)
            progression.append(client.readiness_score)
            assert client.is_ready is (index == len(keys))

    # Then the score walks from 0 to 100 monotonically
    assert progression == [17, 33, 50, 67, 83, 100]


def test_not_applicable_items_leave_the_fraction_rather_than_count_as_done(
    tenant: Tenant,
) -> None:
    # Given a client with two items excluded as not applicable
    assert_isolated_role()
    client = _client(tenant, govbr_trust_level=GovBrTrustLevel.OURO)

    with tenant_context(tenant.id):
        keys = list(client.onboarding_items.values_list("key", flat=True))
        _mark(client, keys[:2], OnboardingStatus.NOT_APPLICABLE)
        _mark(client, keys[2:4], OnboardingStatus.DONE)

        # When readiness is computed
        # Then it is 2 of the 4 that apply, not 4 of 6. Counting the excluded ones as
        # done would report a client as further along than anyone has taken them.
        assert client.readiness_score == 50


def test_a_bronze_account_surfaces_the_ecac_blocker(tenant: Tenant) -> None:
    # Given a client whose gov.br account is bronze
    assert_isolated_role()
    client = _client(tenant, govbr_trust_level=GovBrTrustLevel.BRONZE)

    # When the blocker is read
    with tenant_context(tenant.id):
        blocker = client.ecac_blocker

    # Then it names the tier and how many items are stuck behind it
    assert blocker is not None
    assert "bronze" in blocker
    assert str(ECAC_ITEM_COUNT) in blocker


def test_an_unrecorded_trust_level_blocks_exactly_like_bronze(tenant: Tenant) -> None:
    # Given a client nobody has asked about their gov.br tier
    assert_isolated_role()
    client = _client(tenant)
    assert client.govbr_trust_level == GovBrTrustLevel.UNKNOWN

    # When the blocker is read
    with tenant_context(tenant.id):
        blocker = client.ecac_blocker

    # Then it fires. "Nobody asked" and "asked, and the answer disqualifies them" are
    # both states in which the firm cannot rely on portal access.
    assert blocker is not None


@pytest.mark.parametrize("level", [GovBrTrustLevel.PRATA, GovBrTrustLevel.OURO])
def test_prata_and_ouro_clear_the_blocker(tenant: Tenant, level: str) -> None:
    # Given a client at a tier e-CAC admits
    assert_isolated_role()
    client = _client(tenant, govbr_trust_level=level)

    # When the blocker is read
    # Then there is none, even with every item still outstanding
    with tenant_context(tenant.id):
        assert client.ecac_blocker is None
        assert client.readiness_score == 0


def test_a_bronze_client_with_the_ecac_items_already_done_is_not_blocked(
    tenant: Tenant,
) -> None:
    # Given a bronze client whose e-CAC items were settled another way
    assert_isolated_role()
    client = _client(tenant, govbr_trust_level=GovBrTrustLevel.BRONZE)
    ecac_keys = list(
        OnboardingItemTemplate.objects.filter(requires_ecac=True).values_list(
            "key",
            flat=True,
        ),
    )
    assert len(ecac_keys) == ECAC_ITEM_COUNT

    with tenant_context(tenant.id):
        _mark(client, ecac_keys, OnboardingStatus.DONE)

        # When the blocker is read
        # Then it is gone. The blocker names unreachable work, not a low tier.
        assert client.ecac_blocker is None


def test_the_ecac_flag_reaches_a_new_clients_items(tenant: Tenant) -> None:
    # Given the seeded templates, two of which route through e-CAC
    assert_isolated_role()
    client = _client(tenant)

    # When the client's own checklist is read
    with tenant_context(tenant.id):
        flagged = client.onboarding_items.filter(requires_ecac=True).count()
        total = client.onboarding_items.count()

    # Then the flag was copied onto the items, not left on the template alone
    assert flagged == ECAC_ITEM_COUNT
    assert total == DEFAULT_ITEM_COUNT


def test_no_certificate_material_can_be_stored_anywhere() -> None:
    # Given the client registry
    offenders = [
        f"{ClientCompany.__name__}.{field.name} is a {type(field).__name__}"
        for field in ClientCompany._meta.get_fields()
        if isinstance(field, FORBIDDEN_FIELDS)
    ]

    # When its fields are inspected for anywhere a certificate could be put
    # Then there is none. Certificate custody would make this product a holder of
    # signing keys for every firm on it, which the anchor forbids in v1 — so the
    # column to hold one must not exist, rather than merely go unused.
    assert not offenders, f"certificate material could be stored: {offenders}"
    assert {"has_digital_certificate", "certificate_expires_on"} <= {
        field.name for field in ClientCompany._meta.get_fields()
    }


def test_no_model_in_the_clients_app_can_store_a_file() -> None:
    # Given every table the client registry owns
    app = django_apps.get_app_config("clients")
    offenders = [
        f"{model.__name__}.{field.name} is a {type(field).__name__}"
        for model in app.get_models()
        for field in model._meta.get_fields()
        if isinstance(field, FORBIDDEN_FIELDS)
    ]

    # When all of them are inspected
    # Then none can hold file content. Checking only ClientCompany would be satisfied
    # by moving the field to a neighbouring table.
    assert not offenders, f"file storage in the clients app: {offenders}"


def test_the_score_is_a_pure_function_of_the_checklist() -> None:
    # Given checklist entries with no database behind them
    entries = [
        ChecklistEntry(status=OnboardingStatus.DONE),
        ChecklistEntry(status=OnboardingStatus.PENDING),
        ChecklistEntry(status=OnboardingStatus.NOT_APPLICABLE),
    ]

    # When the score and blocker are computed directly
    # Then both answer from the data alone, which is what makes every branch above
    # cheap to cover and the model property a one-line delegation
    assert score(entries) == 50
    assert score([]) == 0
    assert score([ChecklistEntry(status=OnboardingStatus.NOT_APPLICABLE)]) == 0
    assert (
        ecac_blocker(entries, GovBrTrustLevel.BRONZE, GovBrTrustLevel.below_ecac())
        is None
    )
    assert (
        ecac_blocker(
            [ChecklistEntry(status=OnboardingStatus.PENDING, requires_ecac=True)],
            GovBrTrustLevel.BRONZE,
            GovBrTrustLevel.below_ecac(),
        )
        is not None
    )
