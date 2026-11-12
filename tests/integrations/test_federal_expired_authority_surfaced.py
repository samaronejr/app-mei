"""AXRM-018: an expired PAGTOWEB authority is surfaced, not silently skipped.

Plan todo 18: failures and missing authority surface in Hoje. The sweep treats an
authority that has lapsed like a missing one, so the client is not consulted and
one owned `autorizacao_ausente` problem is opened for it.
"""

from collections.abc import Callable
from datetime import date

import pytest

from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.integrations.authority import (
    Admission,
    authority_for,
    declare_authority,
    head_record,
    verify_authority,
)
from apps.integrations.models import ClientProviderLink, IntegrationIssue
from apps.integrations.serpro.pagtoweb import PAGE_OPERATION
from tests.integrations.pagtoweb_support import World, page, world
from tests.integrations.test_federal_sweeps import DAY, cnpj, fire

__all__ = ["fire", "world"]
pytestmark = pytest.mark.django_db(transaction=True)

# Fixed dates, all before the sweep day: the authority lapsed on January 31 and is
# declared and verified as of January 15, whatever the real date is when this runs.
LAPSED_ON = date(2026, 1, 31)
DECLARED_ON = date(2026, 1, 15)


def test_an_expired_payment_authority_is_surfaced_as_missing_authority(
    world: World, fire: Callable[[date], int]
) -> None:
    termo = world.termo
    with tenant_context(termo.tenant.pk):
        client = ClientCompany.objects.create(
            tenant=termo.tenant,
            legal_name="Autorizacao vencida",
            cnpj=cnpj("AB0999000001"),
            is_mei=True,
        )
        link = ClientProviderLink.objects.create(
            tenant=termo.tenant,
            client=client,
            connection_id=termo.connection.pk,
            provider_company_ref="expired-authority",
            status="ativa",
        )
        declare_authority(
            termo.actor,
            client.pk,
            service="pagtoweb",
            code="00004",
            valid_from=date(2026, 1, 1),
            valid_to=LAPSED_ON,
            expected_version=0,
            today=DECLARED_ON,
        )
        head = head_record(client.pk, "pagtoweb")
        assert head is not None
        verify_authority(
            termo.actor,
            client.pk,
            service="pagtoweb",
            expected_version=head.version,
            today=DECLARED_ON,
        )
        decision = authority_for(client, PAGE_OPERATION, link=link, today=DAY)
    assert decision.state == Admission.EXPIRED
    world.script(page([]))
    assert fire(DAY) == 1
    with tenant_context(termo.tenant.pk):
        surfaced = IntegrationIssue.objects.filter(
            client_id=client.pk, reason="autorizacao_ausente", service="pagtoweb"
        ).count()
    assert surfaced == 1
