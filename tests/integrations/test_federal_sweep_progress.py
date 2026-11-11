"""Whole failed or refused turns must not strand the rest of a firm's clients."""

from typing import TYPE_CHECKING

import pytest

from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.delivery.models import AutomationPause
from apps.integrations.serpro import tasks
from tests.integrations.test_federal_sweeps import (
    DAY,
    PAYMENTS,
    firms,
    lane,
    on,
    sweeps,
)

if TYPE_CHECKING:
    from uuid import UUID

__all__ = ["lane"]
pytestmark = pytest.mark.django_db(transaction=True)


def test_all_twenty_failing_clients_do_not_starve_the_rest(
    lane: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = sweeps()
    made = firms(3, 50)
    visits: list[tuple[UUID, UUID]] = []
    failed = {(tenant, client) for tenant, ids in made.items() for client in ids[:20]}

    def act(client: ClientCompany, window: object) -> object:
        pair = (client.tenant_id, client.pk)
        visits.append(pair)
        if pair in failed:
            message = "persistent per-client failure"
            raise RuntimeError(message)
        return module.Outcome.QUEUED

    monkeypatch.setattr(module, "payments_for", act)
    on(monkeypatch, DAY)
    results = [getattr(tasks, PAYMENTS).apply().get() for _ in range(3)]
    seen = set(visits)
    expected = {(tenant, client) for tenant, ids in made.items() for client in ids}
    assert seen == expected, (
        "a whole failing turn must still advance to clients beyond its first twenty"
    )
    assert results == [0, 60, 30]


@pytest.mark.parametrize("field", ["federal_payments_cursor", "federal_sitfis_cursor"])
@pytest.mark.parametrize("failure", ["exception", "refused"])
def test_both_cursors_advance_through_whole_unsuccessful_turns(
    lane: None, field: str, failure: str
) -> None:
    module = sweeps()
    made = firms(3, 50)
    visits: list[tuple[UUID, UUID]] = []
    blocked = {(tenant, client) for tenant, ids in made.items() for client in ids[:20]}

    def act(client: ClientCompany) -> object:
        pair = (client.tenant_id, client.pk)
        visits.append(pair)
        if pair in blocked:
            if failure == "exception":
                message = "persistent per-client failure"
                raise RuntimeError(message)
            return module.Outcome.REFUSED
        return module.Outcome.QUEUED

    for _ in range(3):
        for tenant in made:
            module._turn(tenant, field, ClientCompany.objects.all, act)
    assert set(visits) == {
        (tenant, client) for tenant, ids in made.items() for client in ids
    }
    for tenant, ids in made.items():
        with tenant_context(tenant):
            control = AutomationPause.objects.get(tenant_id=tenant)
            assert getattr(control, field) == ids[9]
            other = (
                "federal_sitfis_cursor"
                if field == "federal_payments_cursor"
                else "federal_payments_cursor"
            )
            assert getattr(control, other) is None
