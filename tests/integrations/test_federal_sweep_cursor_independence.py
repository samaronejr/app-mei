"""AXRM-018: each sweep keeps its own cursor and visits every client in turn.

Plan todo 18 (fair monthly sweeps): each sweep serves every linked client of a firm in
turn, 20 per run, resuming after its own last client. Both real beat tasks run here
interleaved in an uneven order over firms with more clients than one page, while the
same pool stays eligible (as with handoffs whose workers have not run yet). Per-client
work is replaced by recorders; selection, tenant traversal and cursor writes are real.
"""

from collections import defaultdict
from datetime import timedelta
from uuid import UUID

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

__all__ = ["lane"]
pytestmark = pytest.mark.django_db(transaction=True)

SITFIS = "schedule_situation_reports"
FIELD = {PAYMENTS: "federal_payments_cursor", SITFIS: "federal_sitfis_cursor"}
# Uneven on purpose: a shared cursor cannot match both kinds' own sequences.
TURNS = (SITFIS, SITFIS, PAYMENTS, SITFIS, PAYMENTS, PAYMENTS)


def _cursors(tenants: list[UUID]) -> dict[UUID, dict[str, UUID | None]]:
    """Each firm's two stored cursors, read under that firm's own tenant context."""
    found: dict[UUID, dict[str, UUID | None]] = {}
    for tenant in tenants:
        with tenant_context(tenant):
            row = AutomationPause.objects.get(tenant_id=tenant)
        found[tenant] = {kind: getattr(row, field) for kind, field in FIELD.items()}
    return found


def test_interleaved_sweeps_each_visit_every_client_in_their_own_cursor_order(
    lane: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = sweeps()
    made = firms(2, 30)
    limit = module.CLIENT_LIMIT
    assert all(len(ids) > limit for ids in made.values())
    visits: dict[str, dict[UUID, list[UUID]]] = {k: defaultdict(list) for k in FIELD}
    queued = module.Outcome.QUEUED

    def payment(client: ClientCompany, window: object) -> object:
        visits[PAYMENTS][client.tenant_id].append(client.pk)
        return queued

    def report(client: ClientCompany) -> object:
        visits[SITFIS][client.tenant_id].append(client.pk)
        return queued

    monkeypatch.setattr(module, "payments_for", payment)
    monkeypatch.setattr(module, "report_for", report)
    monkeypatch.setattr(module, "report_candidates", lambda today: module._linked())
    before: dict[UUID, dict[str, UUID | None]] = {t: dict.fromkeys(FIELD) for t in made}
    for day, kind in enumerate(TURNS):
        on(monkeypatch, DAY + timedelta(days=day))
        seen = {tenant: len(visits[kind][tenant]) for tenant in made}
        result = getattr(tasks, kind).apply()
        assert result.successful(), result.result
        assert result.get() == limit * len(made)
        after = _cursors(list(made))
        for tenant in made:
            turn = visits[kind][tenant][seen[tenant] :]
            assert len(turn) == limit, (kind, len(turn))
            # Its own cursor moved to its last client; the other sweep's did not move.
            assert after[tenant][kind] == turn[-1], kind
            other = next(k for k in FIELD if k != kind)
            assert after[tenant][other] == before[tenant][other], (kind, other)
        before = after
    for kind in FIELD:
        runs = TURNS.count(kind)
        for tenant, ids in made.items():
            expected = [ids[i % len(ids)] for i in range(runs * limit)]
            assert visits[kind][tenant] == expected, (kind, tenant)
            # Three runs of 20 over 30 clients: two full cycles, each client twice.
            assert {visits[kind][tenant].count(pk) for pk in ids} == {
                runs * limit // len(ids)
            }
