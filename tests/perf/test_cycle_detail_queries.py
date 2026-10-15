"""Biting growth controls exercise the actual rendered cycle-detail GET."""

import json
import sys

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.models import RequestState
from tests.cycles.display_fixtures import (
    add_contacts,
    add_obligations,
    add_waivers,
    populated_detail,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("collection", ["waivers", "contacts", "obligations"])
def test_cycle_detail_queries_do_not_grow_per_item(
    settings: SettingsWrapper,
    collection: str,
) -> None:
    # Given nonempty collections on both sides, including real approval receipts.
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    world = populated_detail(f"t36-growth-{collection}")
    browser = world.firm()
    path = f"/ciclos/{world.cycle.pk}/"
    assert browser.get(path).status_code == 200
    with CaptureQueriesContext(connection) as small:
        baseline = browser.get(path)
    assert baseline.status_code == 200
    growers = {
        "waivers": add_waivers,
        "contacts": add_contacts,
        "obligations": add_obligations,
    }
    with tenant_context(world.graph.tenant.pk):
        added = growers[collection](world, 1, 10)
    # When ten independently identified rows are added to just one collection.
    with CaptureQueriesContext(connection) as many:
        response = browser.get(path)
    # Then no per-item reads return, and all new rows actually reach the surface.
    assert response.status_code == 200
    counts = {"collection": collection, "small": len(small), "many": len(many)}
    sys.stdout.write(
        f"\nAXR1_CYCLE_DETAIL_GROWTH={json.dumps(counts, sort_keys=True)}\n"
    )
    assert len(many) <= len(small), (
        collection,
        len(small),
        len(many),
        many.captured_queries,
    )
    if collection == "contacts":
        displayed = {row.contact.pk for row in response.context["recipients"]}
        assert len(response.context["recipients"]) == 12
        for recipient in response.context["recipients"]:
            assert recipient.contact.email.encode() in response.content
    elif collection == "obligations":
        displayed = {row.pk for row in response.context["bindable_obligations"]}
        assert len(displayed) == 11
        for identifier in added:
            assert f'value="{identifier}"'.encode() in response.content
    else:
        displayed = {row["request"].pk for row in response.context["evidence_requests"]}
        assert len(displayed) == 12
        for identifier in added:
            assert f'data-request-id="{identifier}"'.encode() in response.content
        waived = [
            row["request"]
            for row in response.context["evidence_requests"]
            if row["request"].state == RequestState.WAIVED
        ]
        assert len(waived) == 11
        assert all(row.required for row in waived)
        waived_keys = {f"requests:{row.pk}:{row.version}" for row in waived}
        assert waived_keys.isdisjoint(
            row.key for row in response.context["completion_items"]
        )
    assert len(added) == 10
    assert added <= displayed
