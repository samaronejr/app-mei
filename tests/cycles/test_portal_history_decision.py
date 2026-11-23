"""A cutover decision is data; calculating it must not persist or expose a row."""

from importlib import import_module
from importlib.util import find_spec
from uuid import UUID

import pytest
from pytest_django.fixtures import DjangoAssertNumQueries


@pytest.mark.django_db
def test_missing_creation_produces_a_private_machine_readable_audit_record(
    django_assert_num_queries: DjangoAssertNumQueries,
) -> None:
    name = "apps.cycles.migrations._request_history"
    assert find_spec(name) is not None
    policy = import_module(name)
    request, tenant, client, cycle, actor = (UUID(int=n) for n in range(1, 6))
    row = policy.Request(
        request, tenant, client, cycle, "unknown display text", 3, actor
    )
    with django_assert_num_queries(0):
        decision = policy.classify(row, [], None, [])
    assert decision is not None
    assert decision.restore is True
    assert decision.audit == {
        "tenant_id": tenant,
        "actor_id": None,
        "action": "cycles.request_visibility_repaired",
        "object_type": "EvidenceRequest",
        "object_id": str(request),
        "metadata": {
            "client_id": str(client),
            "cycle_id": str(cycle),
            "reason": "missing_creation",
            "previous_version": 3,
            "previous_state": "unknown",
        },
    }
