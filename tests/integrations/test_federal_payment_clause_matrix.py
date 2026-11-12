"""Each settled-query predicate has a distinct positive and negative control."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest

from apps.core.tenancy import tenant_context
from apps.integrations.models import ClientProviderLink
from apps.integrations.serpro import sweeps
from apps.integrations.serpro.models import PaymentQuery
from apps.integrations.serpro.pagtoweb import PaymentRange
from tests.integrations.pagtoweb_support import World, world
from tests.integrations.test_federal_sweeps import add_clients

__all__ = ["world"]
if TYPE_CHECKING:
    from uuid import UUID

    from apps.clients.models import ClientCompany

pytestmark = pytest.mark.django_db(transaction=True)
WINDOW = PaymentRange(date(2026, 9, 1), date(2026, 10, 21))


@pytest.mark.parametrize("clause", ["client", "trigger", "month", "state"])
def test_only_a_settled_scheduled_query_for_this_client_and_month_spends_its_slot(
    world: World,
    clause: str,
) -> None:
    other = add_clients(world, 1, mei=True)[0]
    with tenant_context(world.termo.tenant.pk):
        # The same firm owns both clients; each query must use its client's link.
        link = ClientProviderLink.objects.get(client=other)
        PaymentQuery.objects.create(
            tenant_id=other.tenant_id,
            client=other,
            link=link,
            trigger="equipe" if clause == "trigger" else "agendada",
            requested_by=world.termo.actor if clause == "trigger" else None,
            request_key=f"clause-{clause}",
            range_start=date(2026, 8, 1) if clause == "month" else WINDOW.start,
            range_end=WINDOW.end,
            environment="fake",
            state="na_fila" if clause == "state" else "nada_localizado",
        )
        expected = {world.termo.client.pk}
        if clause != "client":
            expected.add(other.pk)
        assert (
            set(sweeps.payment_candidates(WINDOW).values_list("pk", flat=True))
            == expected
        )
    visited: list[UUID] = []

    def record(client: ClientCompany) -> sweeps.Outcome:
        visited.append(client.pk)
        return sweeps.Outcome.QUEUED

    for _day in range(21, 32):
        queued, failed = sweeps._turn(
            world.termo.tenant.pk,
            "federal_payments_cursor",
            lambda: sweeps.payment_candidates(WINDOW),
            record,
        )
        assert failed == 0
        assert queued == len(expected)
        assert set(visited) <= expected
    assert set(visited) == expected
