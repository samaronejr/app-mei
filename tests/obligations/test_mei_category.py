"""The MEI category on the client record, which decides which ceiling applies.

MEI-Caminhoneiro carries a substantially higher annual ceiling than common MEI. A
registry with no category column forces every threshold check to assume the common
one, which reports a compliant trucker as over the limit — and there is no later
signal that the answer was wrong, because the number itself is plausible.
"""

import pytest

from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.fiscal.mei import MEICategory
from apps.tenants.models import Tenant

pytestmark = pytest.mark.django_db


def test_a_new_client_is_a_common_mei_by_default() -> None:
    # Given a firm
    tenant = Tenant.objects.create(name="Alpha", slug="alpha-mei-cat")

    # When a client is registered without stating a category
    with tenant_context(tenant.id):
        client = ClientCompany.objects.create(
            tenant=tenant,
            legal_name="Padaria do Zé MEI",
            cnpj="11222333000181",
        )

    # Then it is a common MEI. The overwhelming majority are, and defaulting to the
    # higher caminhoneiro ceiling would understate consumption for everyone else.
    assert client.mei_category == MEICategory.COMMON


def test_a_client_can_be_recorded_as_a_caminhoneiro() -> None:
    # Given a firm
    tenant = Tenant.objects.create(name="Beta", slug="beta-mei-cat")

    # When a trucker is registered
    with tenant_context(tenant.id):
        client = ClientCompany.objects.create(
            tenant=tenant,
            legal_name="Transportes MEI",
            cnpj="11222333000181",
            mei_category=MEICategory.CAMINHONEIRO,
        )
        reloaded = ClientCompany.objects.get(pk=client.pk)

    # Then the category survives the round trip and is available to the resolver
    assert reloaded.mei_category == MEICategory.CAMINHONEIRO


def test_the_two_categories_are_the_only_ones() -> None:
    # Given the category enumeration
    # When its values are listed
    # Then there are exactly two. A third would need its own seeded ceiling row, so
    # adding one silently would make the resolver raise NoEffectiveParameter for it.
    assert set(MEICategory.values) == {"common", "caminhoneiro"}
