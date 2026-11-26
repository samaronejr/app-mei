"""A leftover payment page is listed with its true condition (AXRM-051 B6).

A page of a consultation that has ended is removed by the next sweep; it is not
promised a further seven days. A page of an active consultation lasts at most its
seven-day ceiling.
"""

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.integrations.call_models import ProviderCall
from apps.integrations.serpro.models import PaymentPage, PaymentQuery
from tests.integrations.pagtoweb_support import World, page, world
from tests.integrations.pagtoweb_support import item as arrecadacao

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["world"]


def test_a_page_of_an_ended_query_is_not_promised_seven_more_days(
    world: World,
) -> None:
    tenant_id = world.termo.tenant.pk
    world.script(page([arrecadacao("DAS000000000051")]))
    world.consult()
    with tenant_context(tenant_id):
        query = PaymentQuery.objects.get()
        left = PaymentPage.objects.create(
            tenant_id=tenant_id,
            client_id=world.termo.client.pk,
            query=query,
            call=ProviderCall.objects.get(operation="SP-PAG"),
            index=0,
            records=[],
        )
    out = StringIO()
    call_command("retention_inventory", "--tenant", str(tenant_id), stdout=out)
    [line] = [
        text
        for text in out.getvalue().splitlines()
        if text.startswith("RECORD kind=payment_page") and str(left.pk) in text
    ]
    assert line.endswith("condition=removed-on-next-sweep")
    ceiling = timezone.localtime(left.created_at + timedelta(days=7)).isoformat()
    assert ceiling not in line
