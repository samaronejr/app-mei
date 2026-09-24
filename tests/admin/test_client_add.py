"""The admin add form's tenant stamp, exercised through the real request path.

`ClientCompanyForm` omits `tenant` deliberately — an editable dropdown would be a
cross-tenant move primitive — so the admin's `save_model` is the only thing that can
put a firm on the INSERT. When it did not, the row went to PostgreSQL with
`tenant_id = NULL`, the row-level-security predicate `tenant_id = app.tenant_id`
evaluated `NULL = <uuid>` — never true — and the add died on the policy.

The test therefore POSTs to the add view rather than calling `save_model` directly:
the defect lives in the seam between the middleware's `tenant_context` and the
INSERT, and a unit test of either side alone would not have caught it.
"""

from http import HTTPStatus

import pytest
from django.contrib.auth.models import Permission
from django.test import Client
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import ClientCompany, ClientStatus, GovBrTrustLevel
from apps.core.tenancy import tenant_context
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.support import enrol_totp

pytestmark = pytest.mark.django_db(transaction=True)

SELECT_URL = "/admin/selecionar-empresa/"
ADD_URL = "/admin/clients/clientcompany/add/"

# RFB's own worked example for the alphanumeric CNPJ — a number the check-digit
# validator accepts, so the POST reaches the INSERT rather than dying on the form.
VALID_CNPJ = "12ABC34501DE35"
# A valid CPF is sent rather than omitted: the form normalizes an empty cpf to "",
# which the `clientcompany_cpf_normalized` CHECK (NULL or eleven characters, never
# "") rejects as a form error before the INSERT is ever attempted.
VALID_CPF = "52998224725"


@pytest.fixture(autouse=True)
def _urls(settings: SettingsWrapper) -> None:
    settings.ROOT_URLCONF = "tests.admin.urls"
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]


@pytest.fixture
def operator_client() -> tuple[Client, Tenant]:
    """A signed-in operator holding `add_clientcompany` on one selected firm."""
    tenant = Tenant.objects.create(name="Alpha", slug="alpha")
    operator = User.objects.create_user(email="op@platform.example", is_staff=True)
    operator.user_permissions.add(
        *Permission.objects.filter(
            content_type__app_label="clients",
            codename__in=("add_clientcompany", "view_clientcompany"),
        ),
    )
    enrol_totp(operator)
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


def test_adding_a_client_stamps_the_selected_firm_on_the_row(
    operator_client: tuple[Client, Tenant],
) -> None:
    client, tenant = operator_client

    # Given the operator has selected Alpha and submits the add form
    response = client.post(
        ADD_URL,
        {
            "legal_name": "Padaria do Ze MEI",
            "cnpj": VALID_CNPJ,
            "cpf": VALID_CPF,
            "status": ClientStatus.ONBOARDING,
            "govbr_trust_level": GovBrTrustLevel.UNKNOWN,
            "is_mei": "on",
        },
    )

    # Then the row is created rather than rejected by row-level security. Before the
    # tenant stamp this POST raised ProgrammingError: the INSERT carried
    # tenant_id=NULL, and `NULL = app.tenant_id` is never true.
    assert response.status_code == HTTPStatus.FOUND
    with tenant_context(tenant.id):
        created = ClientCompany.objects.get(cnpj=VALID_CNPJ)
    assert created.tenant_id == tenant.id
