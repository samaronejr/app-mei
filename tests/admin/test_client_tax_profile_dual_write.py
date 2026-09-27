"""The admin's dual-write: the legacy flag and the timeline land as one claim.

While `is_mei` and `mei_category` still exist on the form, every save through the
client admin must also carry the claim into `clients_taxprofile` — through
`sync_profile_flag`, which delegates to `correct_tax_profile`, so the write is
locked, audited and capability-checked exactly as a deliberate correction is.
Without it, a save that flips the flag would leave today's readers and the
timeline disagreeing until somebody noticed.

The assertions POST through the real add/change views rather than calling
`save_model` directly, for the same reason the tenant-stamp test does: the seam
under test is middleware context plus form plus service, and unit-testing either
side alone is where this defect used to live.
"""

from datetime import date
from http import HTTPStatus

import pytest
from django.contrib.auth.models import Permission
from django.test import Client
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import (
    ClientCompany,
    ClientStatus,
    GovBrTrustLevel,
    TaxProfile,
    TaxRegime,
)
from apps.clients.services import profile_at
from apps.core.tenancy import tenant_context
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.support import enrol_totp

pytestmark = pytest.mark.django_db(transaction=True)

SELECT_URL = "/admin/selecionar-empresa/"
ADD_URL = "/admin/clients/clientcompany/add/"
CHANGE_URL = "/admin/clients/clientcompany/{pk}/change/"

VALID_CNPJ = "12ABC34501DE35"
VALID_CPF = "52998224725"


@pytest.fixture(autouse=True)
def _urls(settings: SettingsWrapper) -> None:
    settings.ROOT_URLCONF = "tests.admin.urls"
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]


@pytest.fixture
def operator_client() -> tuple[Client, Tenant]:
    """A signed-in operator with add/change on clients of one selected firm."""
    tenant = Tenant.objects.create(name="Alpha", slug="alpha-dual")
    operator = User.objects.create_user(email="op@platform.example", is_staff=True)
    operator.user_permissions.add(
        *Permission.objects.filter(
            content_type__app_label="clients",
            codename__in=(
                "add_clientcompany",
                "change_clientcompany",
                "view_clientcompany",
            ),
        ),
    )
    enrol_totp(operator)
    # OWNER holds clients.edit_tax_profile FULL, which the correction service
    # re-checks inside its lock — the dual-write keeps the same gate.
    Membership.objects.create(
        user=operator,
        tenant=tenant,
        role=TenantRole.OWNER,
    )
    client = Client()
    client.force_login(operator)
    response = client.post(SELECT_URL, {"tenant": str(tenant.pk), "next": ADD_URL})
    assert response.status_code == HTTPStatus.FOUND, "tenant selection was refused"
    return client, tenant


def _base_form(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "legal_name": "Padaria do Ze MEI",
        "cnpj": VALID_CNPJ,
        "cpf": VALID_CPF,
        "status": ClientStatus.ONBOARDING,
        "govbr_trust_level": GovBrTrustLevel.UNKNOWN,
    }
    data.update(overrides)
    return data


def test_adding_a_mei_client_writes_the_timeline_claim(
    operator_client: tuple[Client, Tenant],
) -> None:
    http, tenant = operator_client
    today = timezone.localdate()

    response = http.post(
        ADD_URL,
        # ISO input is always valid for a DateField regardless of the active
        # locale's display formats — the test pins behaviour, not pt-BR parsing.
        _base_form(is_mei="on", opened_on="2020-03-15"),
    )

    assert response.status_code == HTTPStatus.FOUND
    with tenant_context(tenant.id):
        client = ClientCompany.objects.get(cnpj=VALID_CNPJ)
        claims = list(TaxProfile.objects.filter(client=client))
        assert len(claims) == 1
        claim = claims[0]
        assert claim.regime == TaxRegime.MEI
        assert claim.mei_category == client.mei_category
        # The claim opens at the earlier of opening and today — the same window
        # the backfill asserted — and answers MEI for today.
        assert claim.valid_from == date(2020, 3, 15)
        assert profile_at(client, today) is not None
        assert profile_at(client, today).regime == TaxRegime.MEI  # type: ignore[union-attr]
        assert claim.provenance == "dual_write"


def test_adding_a_non_mei_client_writes_an_unknown_claim(
    operator_client: tuple[Client, Tenant],
) -> None:
    http, tenant = operator_client

    response = http.post(ADD_URL, _base_form())

    assert response.status_code == HTTPStatus.FOUND
    with tenant_context(tenant.id):
        client = ClientCompany.objects.get(cnpj=VALID_CNPJ)
        claim = TaxProfile.objects.get(client=client)
        # The flag's absence maps to unknown, never to an invented regime — the
        # same rule the backfill applied.
        assert claim.regime == TaxRegime.UNKNOWN
        assert claim.mei_category is None


def test_flipping_the_flag_supersedes_the_claim_from_today(
    operator_client: tuple[Client, Tenant],
) -> None:
    http, tenant = operator_client
    today = timezone.localdate()

    http.post(ADD_URL, _base_form(is_mei="on"))
    with tenant_context(tenant.id):
        client = ClientCompany.objects.get(cnpj=VALID_CNPJ)
        pk = client.pk

    response = http.post(
        CHANGE_URL.format(pk=pk),
        _base_form(tax_profile_revision=client.tax_profile_revision),
    )

    assert response.status_code == HTTPStatus.FOUND
    with tenant_context(tenant.id):
        claims = list(TaxProfile.objects.filter(client_id=pk).order_by("recorded_at"))
        assert len(claims) == 2
        superseded, current = claims
        assert superseded.regime == TaxRegime.MEI
        assert superseded.superseded_at is not None
        assert current.regime == TaxRegime.UNKNOWN
        # The edit asserts "from today": history stays with the MEI claim that was
        # believed then, and the flag's new answer governs only what comes next.
        assert current.valid_from == today
        assert profile_at(ClientCompany.objects.get(pk=pk), today).regime == (  # type: ignore[union-attr]
            TaxRegime.UNKNOWN
        )


def test_an_edit_that_changes_nothing_writes_no_row(
    operator_client: tuple[Client, Tenant],
) -> None:
    http, tenant = operator_client

    http.post(ADD_URL, _base_form(is_mei="on"))
    with tenant_context(tenant.id):
        client = ClientCompany.objects.get(cnpj=VALID_CNPJ)
        pk = client.pk

    response = http.post(
        CHANGE_URL.format(pk=pk),
        _base_form(
            is_mei="on",
            legal_name="Padaria do Ze MEI Renomeada",
            tax_profile_revision=client.tax_profile_revision,
        ),
    )

    assert response.status_code == HTTPStatus.FOUND
    with tenant_context(tenant.id):
        # One claim was already the answer for today; a save that restates it adds
        # nothing. Dual-write is an invariant over content, not a write-per-save.
        assert TaxProfile.objects.filter(client_id=pk).count() == 1
