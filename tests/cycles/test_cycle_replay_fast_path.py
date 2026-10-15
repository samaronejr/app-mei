"""Replays bypass creation eligibility; stale misses still hit the real unique key."""

from unittest.mock import patch

import pytest
from django.db import connection
from django.db.models import Model, QuerySet
from django.test.utils import CaptureQueriesContext

from apps.clients.models import Engagement
from apps.clients.services import profile_at
from apps.core.tenancy import tenant_context
from apps.cycles.models import CycleEvent, ServiceCycle
from apps.cycles.services import cycle as service
from apps.cycles.targeting import AppliesTo
from tests.cycles.cycle_fixtures import PERIOD, cycle_setup

pytestmark = pytest.mark.django_db(transaction=True)


def test_existing_replay_never_evaluates_creation_eligibility() -> None:
    setup = cycle_setup()
    with (
        tenant_context(setup.tenant.pk),
        patch("apps.cycles.services.cycle.profile_at", wraps=profile_at) as profile,
        patch.object(AppliesTo, "parse", wraps=AppliesTo.parse) as scope,
    ):
        first = service.open_cycle(
            setup.actor,
            setup.engagement.pk,
            PERIOD,
            setup.definition.pk,
            idempotency_key="fast-path",
        )
        assert first.created
        # Non-vacuity: these spies actually observe the real creation checks.
        assert profile.call_count == 1
        assert scope.call_count == 1
        cycle = ServiceCycle.objects.get(pk=first.id)
        graph = list(cycle.tasks.order_by("pk").values())
        for service_kind in ["mei_mensal", "documentos_mensal"]:
            Engagement.objects.filter(pk=setup.engagement.pk).update(
                service_kind=service_kind
            )
            profile.reset_mock()
            scope.reset_mock()
            with CaptureQueriesContext(connection) as queries:
                replay = service.open_cycle(
                    setup.actor,
                    setup.engagement.pk,
                    PERIOD,
                    setup.definition.pk,
                    idempotency_key="fast-path",
                )
            assert replay.id == first.id
            assert not replay.created
            assert profile.call_count == 0
            assert scope.call_count == 0
            assert not any('"clients_taxprofile"' in query["sql"] for query in queries)
            assert not any(
                query["sql"].lstrip().upper().startswith("INSERT") for query in queries
            )
            assert list(cycle.tasks.order_by("pk").values()) == graph
            assert CycleEvent.objects.filter(cycle=cycle).count() == 1


def test_insert_conflict_reloads_winner_after_a_stale_initial_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = cycle_setup()
    with tenant_context(setup.tenant.pk):
        winner = service.open_cycle(
            setup.actor,
            setup.engagement.pk,
            PERIOD,
            setup.definition.pk,
            idempotency_key="winner",
        )
        graph = list(
            ServiceCycle.objects.get(pk=winner.id).tasks.order_by("pk").values()
        )
        first = QuerySet.first
        missed = False

        def stale_first[Row: Model](queryset: QuerySet[Row]) -> Row | None:
            nonlocal missed
            if queryset.model is ServiceCycle and not missed:
                # Simulate a miss before another writer wins. Only that initial
                # read is displaced: INSERT, constraint, rollback and re-read are
                # real PostgreSQL operations under app_runtime, not mocked.
                missed = True
                return None
            return first(queryset)

        monkeypatch.setattr(QuerySet, "first", stale_first)
        with CaptureQueriesContext(connection) as queries:
            replay = service.open_cycle(
                setup.actor,
                setup.engagement.pk,
                PERIOD,
                setup.definition.pk,
                idempotency_key="loser",
            )
        assert missed
        assert replay.id == winner.id
        assert not replay.created
        assert any("ROLLBACK TO SAVEPOINT" in query["sql"] for query in queries)
        assert ServiceCycle.objects.count() == 1
        assert CycleEvent.objects.count() == 1
        assert (
            list(ServiceCycle.objects.get(pk=winner.id).tasks.order_by("pk").values())
            == graph
        )
