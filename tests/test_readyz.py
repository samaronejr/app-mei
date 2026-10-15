"""The /readyz probe reports whether its required dependencies can serve traffic."""

from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Final
from unittest.mock import patch

import pytest
from django.conf import settings
from django.core.cache import caches
from django.db import (
    DatabaseError,
    ProgrammingError,
    connection,
    connections,
    transaction,
)
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper
from redis.exceptions import RedisError

from apps.clients.models import ClientContact
from apps.core.tenancy import tenant_context
from apps.delivery.models import MessageState, OutboundMessage
from apps.delivery.notice_authority import configure
from apps.delivery.outbox import enqueue
from apps.delivery.policy import BusinessIntent
from tests.cycles.cycle_fixtures import PERIOD, cycle_setup

URLCONF_HOSTS: Final[tuple[tuple[str, str], ...]] = (
    ("config.urls", "testserver"),
    ("config.urls", "alpha.localhost"),
    ("apps.portal.urls", "alpha-portal.localhost"),
)


@pytest.fixture(autouse=True)
def _allowed_hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(("urlconf", "host"), URLCONF_HOSTS)
def test_readyz_reports_ready_when_both_dependencies_respond(
    client: Client,
    urlconf: str,
    host: str,
) -> None:
    # Given the public readiness route on either host's URL tree
    url = reverse("readyz", urlconf=urlconf)

    # When both required dependencies respond
    response = client.get(url, headers={"host": host})

    # Then the service is ready to receive traffic.
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "status": "ready",
        "database": "ok",
        "redis": "ok",
    }


def test_monitoring_probes_are_exempt_from_access_logging() -> None:
    # Given the monitoring-only readiness and release paths
    monitoring_paths = {"/readyz", "/versionz"}

    # When the infrastructure access-log exemptions are inspected
    exempt_paths = set(settings.ACCESS_LOG_EXEMPT_PREFIXES)

    # Then neither probe creates a person-level access record.
    assert monitoring_paths <= exempt_paths


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(("urlconf", "host"), URLCONF_HOSTS)
def test_readyz_reports_unready_when_database_is_unavailable(
    client: Client,
    urlconf: str,
    host: str,
) -> None:
    # Given a database connection failure on either host's readiness route
    url = reverse("readyz", urlconf=urlconf)

    # When the dependency probe tries to open its cursor
    with patch.object(
        connections["default"],
        "cursor",
        side_effect=DatabaseError("database unavailable"),
    ):
        response = client.get(url, headers={"host": host})

    # Then readiness fails while the independent Redis result remains visible.
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json() == {
        "status": "unready",
        "database": "error",
        "redis": "ok",
    }


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(("urlconf", "host"), URLCONF_HOSTS)
def test_readyz_reports_unready_when_redis_is_unavailable(
    client: Client,
    urlconf: str,
    host: str,
) -> None:
    # Given a Redis failure on either host's readiness route
    url = reverse("readyz", urlconf=urlconf)

    # When the cache roundtrip tries to write its probe value
    with patch.object(
        caches["default"],
        "set",
        side_effect=RedisError("redis unavailable"),
    ):
        response = client.get(url, headers={"host": host})

    # Then readiness fails while the independent database result remains visible.
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json() == {
        "status": "unready",
        "database": "ok",
        "redis": "error",
    }


@pytest.mark.django_db(transaction=True)
def test_readyz_propagates_programming_errors(client: Client) -> None:
    # Given a programming error rather than an operational Redis failure
    programming_error = TypeError("programming bug")

    # When it occurs inside the cache roundtrip
    with (
        patch.object(caches["default"], "set", side_effect=programming_error),
        pytest.raises(TypeError, match="programming bug"),
    ):
        client.get(reverse("readyz"))

    # Then Django receives the error instead of a comforting 503 readiness body.


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(("urlconf", "host"), URLCONF_HOSTS)
def test_readyz_opens_no_tenant_transaction(
    client: Client,
    urlconf: str,
    host: str,
) -> None:
    # Given the readiness route on either host's URL tree
    url = reverse("readyz", urlconf=urlconf)

    # When a monitor polls it
    with CaptureQueriesContext(connections["default"]) as captured:
        response = client.get(url, headers={"host": host})

    # Then one fixed aggregate SELECT runs: no tenant GUC or transaction is opened.
    statements = [query["sql"] for query in captured.captured_queries]
    assert response.status_code == HTTPStatus.OK
    assert statements == ["SELECT * FROM public.delivery_outbox_health()"]


NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)


@pytest.fixture
def queued_outbox(settings: SettingsWrapper) -> OutboundMessage:
    settings.OUTBOX_MAX_QUEUED_AGE_SECONDS = 60
    settings.OUTBOX_MAX_DEAD_LETTERS = 0
    graph = cycle_setup("readyz-outbox")
    with tenant_context(graph.tenant.pk):
        contact = ClientContact.objects.create(
            tenant=graph.tenant,
            client=graph.client,
            name="Synthetic readiness contact",
            email="readiness@example.test",
        )
        configure(graph.actor, kinds=("notice",), acknowledged=True, expected_version=0)
        return enqueue(
            graph.actor,
            graph.client.pk,
            contact.pk,
            intent=BusinessIntent(
                graph.engagement.pk,
                graph.definition.playbook.pk,
                PERIOD,
                PERIOD,
                0,
                "readyz",
                "email_notice",
            ),
            expected_version=1,
        )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("age", "expected"),
    [(timedelta(seconds=60), 200), (timedelta(seconds=61), 503)],
)
def test_readyz_oldest_queued_boundary(
    client: Client, queued_outbox: OutboundMessage, age: timedelta, expected: int
) -> None:
    # Given a queued envelope under a real tenant RLS context.
    with tenant_context(queued_outbox.tenant.pk):
        OutboundMessage.objects.filter(pk=queued_outbox.pk).update(created_at=NOW - age)

    # When the public probe executes as app_runtime without tenant context.
    with patch("django.utils.timezone.now", return_value=NOW):
        response = client.get("/readyz")

    # Then the configured age boundary is exact and no private values escape.
    assert response.status_code == expected
    if expected == 503:
        assert response.json()["reason"] == "outbox_oldest_queued_age"
    assert str(queued_outbox.tenant.pk).encode() not in response.content
    assert str(queued_outbox.pk).encode() not in response.content
    assert b"readiness@example.test" not in response.content


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("state", [MessageState.REJECTED, MessageState.UNKNOWN])
def test_readyz_dead_letter_threshold(
    client: Client,
    queued_outbox: OutboundMessage,
    settings: SettingsWrapper,
    state: str,
) -> None:
    # Given one unresolved dead letter, not an old queued envelope.
    with tenant_context(queued_outbox.tenant.pk):
        OutboundMessage.objects.filter(pk=queued_outbox.pk).update(
            state=state, created_at=NOW - timedelta(days=2)
        )

    # When zero dead letters are tolerated.
    response = client.get("/readyz")

    # Then the dead-letter signal, not the queue age, explains the 503.
    assert response.status_code == 503
    assert response.json()["reason"] == "outbox_dead_letters"
    settings.OUTBOX_MAX_DEAD_LETTERS = 1
    assert client.get("/readyz").status_code == 200


@pytest.mark.django_db(transaction=True)
def test_readyz_temporary_backoff_still_counts_as_backlog(
    client: Client, queued_outbox: OutboundMessage
) -> None:
    # Given an old message whose automatic backoff is scheduled in the future.
    with tenant_context(queued_outbox.tenant.pk):
        OutboundMessage.objects.filter(pk=queued_outbox.pk).update(
            state=MessageState.TEMPORARY,
            created_at=NOW - timedelta(minutes=2),
            next_attempt_at=NOW + timedelta(hours=1),
        )

    # When readiness asks about the oldest queued work.
    with patch("django.utils.timezone.now", return_value=NOW):
        response = client.get("/readyz")

    # Then delaying a retry cannot hide its growing backlog age.
    assert response.status_code == 503
    assert response.json()["reason"] == "outbox_oldest_queued_age"


@pytest.mark.django_db(transaction=True)
def test_readyz_zero_age_threshold_is_not_disabled(
    client: Client, queued_outbox: OutboundMessage, settings: SettingsWrapper
) -> None:
    # Given a nonempty queue and a deliberately strict zero threshold.
    settings.OUTBOX_MAX_QUEUED_AGE_SECONDS = 0
    with tenant_context(queued_outbox.tenant.pk):
        OutboundMessage.objects.filter(pk=queued_outbox.pk).update(
            created_at=NOW - timedelta(microseconds=1)
        )

    # When the operator's failure scenario polls the probe.
    with patch("django.utils.timezone.now", return_value=NOW):
        response = client.get("/readyz")

    # Then zero means no tolerated queue delay, not "turn monitoring off".
    assert response.status_code == 503
    assert response.json()["reason"] == "outbox_oldest_queued_age"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "state",
    [
        MessageState.CANCELLED,
        MessageState.ACCEPTED,
        MessageState.MANUAL_SENT,
        MessageState.MANUAL_NOT_SENT,
    ],
)
def test_readyz_resolved_history_does_not_keep_monitor_red(
    client: Client, queued_outbox: OutboundMessage, state: str
) -> None:
    # Given only old, resolved history.
    with tenant_context(queued_outbox.tenant.pk):
        OutboundMessage.objects.filter(pk=queued_outbox.pk).update(
            state=state, created_at=NOW - timedelta(days=2)
        )

    # When readiness polls.
    response = client.get("/readyz")

    # Then neither backlog nor unresolved dead-letter thresholds trip.
    assert response.status_code == 200


@pytest.mark.django_db(transaction=True)
def test_portal_cannot_call_platform_outbox_aggregate(
    queued_outbox: OutboundMessage,
) -> None:
    # Given the private operational aggregate.
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE app_portal")

        # When the portal attempts to call it directly.
        with (
            pytest.raises(ProgrammingError, match="permission denied"),
            transaction.atomic(),
        ):
            cursor.execute("SELECT * FROM public.delivery_outbox_health()")

    # Then function execution is denied, not merely hidden by RLS.
