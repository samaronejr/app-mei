"""Startup must fail loudly when the two load-bearing settings are wrong.

Both failures are otherwise silent. Without `TenantMiddleware` no request ever sets
`app.tenant_id`, so every policy denies every row and the product looks empty rather
than broken. Without `ATOMIC_REQUESTS` the view is not a savepoint, so a request that
converts an exception into a 4xx commits its partial writes.
"""

from http import HTTPStatus

import pytest
from django.db import connections
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext

from apps.core.checks import (
    AUTHENTICATION_MIDDLEWARE,
    TENANT_MIDDLEWARE,
    check_tenant_middleware,
    check_transaction_and_cookie_policy,
)
from config.settings.base import MIDDLEWARE


def _middleware_check_ids() -> set[str | None]:
    return {message.id for message in check_tenant_middleware(None)}


def test_the_shipped_middleware_stack_passes() -> None:
    # Given the settings as shipped
    # When the guard runs
    # Then nothing is reported
    assert check_tenant_middleware(None) == []


def test_the_tenant_middleware_is_registered_after_authentication() -> None:
    # Given the shipped middleware stack
    # When the two positions are compared
    # Then tenant resolution runs after request.user exists, or the membership check
    # would silently pass for every caller
    assert TENANT_MIDDLEWARE in MIDDLEWARE
    assert MIDDLEWARE.index(TENANT_MIDDLEWARE) > MIDDLEWARE.index(
        AUTHENTICATION_MIDDLEWARE,
    )


@override_settings(
    MIDDLEWARE=[m for m in MIDDLEWARE if m != TENANT_MIDDLEWARE],
)
def test_a_missing_tenant_middleware_is_rejected() -> None:
    # Given a stack with the tenant middleware removed
    # When the guard runs
    # Then core.E005 fires
    assert "core.E005" in _middleware_check_ids()


@override_settings(
    MIDDLEWARE=[
        TENANT_MIDDLEWARE,
        *[m for m in MIDDLEWARE if m != TENANT_MIDDLEWARE],
    ],
)
def test_tenant_middleware_before_authentication_is_rejected() -> None:
    # Given the tenant middleware placed ahead of AuthenticationMiddleware
    # When the guard runs
    # Then core.E006 fires
    assert "core.E006" in _middleware_check_ids()


def test_atomic_requests_is_true_on_the_connection() -> None:
    # Given the configured default database
    # When the per-database key is read
    # Then it is True. Read from the connection, never from settings.ATOMIC_REQUESTS,
    # which a module-level assignment would make report True vacuously.
    assert connections["default"].settings_dict["ATOMIC_REQUESTS"] is True
    assert check_transaction_and_cookie_policy(None) == []


@pytest.mark.django_db(transaction=True)
def test_the_liveness_probe_opens_no_tenant_transaction(client: Client) -> None:
    # Given the health endpoint, deliberately marked non_atomic_requests so that a
    # database blip can never make an orchestrator restart a healthy web process
    # When it is probed
    with CaptureQueriesContext(connections["default"]) as captured:
        response = client.get("/healthz")

    # Then the tenant middleware issued nothing at all. set_config is its fingerprint
    # -- an unconditional transaction.atomic() in the middleware would apply the GUC
    # and show up right here.
    assert response.status_code == HTTPStatus.OK
    statements = [query["sql"] for query in captured.captured_queries]
    assert not any("set_config" in sql for sql in statements), (
        f"the tenant middleware opened its transaction on the probe: {statements}"
    )

    # ...and the ONE statement the probe does make is the dead-man's-switch read and
    # nothing else. T-041 requires /healthz to answer 503 when beat goes quiet, which
    # cannot be known without asking; naming the permitted query keeps that from
    # becoming a licence for the next person to add a second one.
    #
    # It stays at ONE statement now that the probe reports backup freshness as well,
    # because both signals are rows of the same name-keyed table and are read by a
    # single `name__in` filter. A second signal that needed a second query would have
    # to justify itself here first.
    assert len(statements) == 1, f"the probe made unexpected queries: {statements}"
    assert "schedulerheartbeat" in statements[0].lower()
