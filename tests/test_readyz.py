"""The /readyz probe reports whether its required dependencies can serve traffic."""

from http import HTTPStatus
from typing import Final
from unittest.mock import patch

import pytest
from django.conf import settings
from django.core.cache import caches
from django.db import DatabaseError, connections
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper
from redis.exceptions import RedisError

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

    # Then only the readiness SELECT runs: no tenant GUC or transaction is opened.
    statements = [query["sql"] for query in captured.captured_queries]
    assert response.status_code == HTTPStatus.OK
    assert statements == ["SELECT 1"]
