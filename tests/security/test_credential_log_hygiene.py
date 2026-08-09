from __future__ import annotations

import json
import re
from http import HTTPStatus
from http import client as http_client
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlencode, urlsplit

import pytest
import sentry_sdk
from django.http import HttpRequest, HttpResponse
from django.middleware.csrf import get_token
from django.test import Client, override_settings
from django.urls import path
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from sentry_sdk.scrubber import EventScrubber
from sentry_sdk.transport import Transport

if TYPE_CHECKING:
    from pytest_django.live_server_helper import LiveServer
    from sentry_sdk.envelope import Envelope
    from sentry_sdk.types import Event

from config.settings import base as base_settings
from config.settings import prod as prod_settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROD_COMPOSE = PROJECT_ROOT / "docker-compose.prod.yml"

REQUIRED_GUNICORN_ATOMS = frozenset(
    {
        "%(h)s",
        "%(t)s",
        "%(m)s",
        "%(s)s",
        "%(b)s",
        "%(L)s",
        "%(a)s",
    }
)
FORBIDDEN_GUNICORN_ATOMS = frozenset({"%(r)s", "%(U)s", "%(q)s"})
SENTRY_URL_CANARY = "CANARY_SENTRY_URL_7c93f"
SENTRY_REAL_URL_CANARY = "CANARY_REAL_URL_8d04a"
SENTRY_REFERER_CANARY = "CANARY_REFERER_a16b2"
SENTRY_PASSWORD1_CANARY = "CANARY_PASSWORD1_b27c3"
SENTRY_PASSWORD2_CANARY = "CANARY_PASSWORD2_c38d4"
SENTRY_OLDPASSWORD_CANARY = "CANARY_OLDPASSWORD_d49e5"
SENTRY_TOTP_CODE_CANARY = "CANARY_TOTP_CODE_e50f6"
SENTRY_TRANSACTION_CANARY = "CANARY_TRANSACTION_f61a7"
SENTRY_BREADCRUMB_CANARY = "CANARY_BREADCRUMB_072b8"
SENTRY_SPAN_CANARY = "CANARY_SPAN_183c9"
SENTRY_RECOVERY_PATH_CANARY = "CANARY_RECOVERY_PATH_294d0"
SENTRY_RECOVERY_CODE_CANARY = "CANARY_RECOVERY_CODE_3a5e1"
SENTRY_TEST_DSN = "http://public@example.invalid/1"
REFERRER_CANARY = "CANARY_REFERRER_PATH_7c93f"
SECURITY_TEST_MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
]


@ensure_csrf_cookie
def _credential_form(request: HttpRequest, credential: str) -> HttpResponse:
    if request.method == "POST":
        return HttpResponse("accepted")
    return HttpResponse(get_token(request))


@csrf_exempt
def _credential_exception(request: HttpRequest, key: str) -> HttpResponse:
    leaked_alias = key
    transaction_url = request.POST.get("transaction_url")
    if transaction_url:
        sentry_sdk.set_transaction_name(transaction_url)
    breadcrumb_url = request.POST.get("breadcrumb_url")
    if breadcrumb_url:
        sentry_sdk.add_breadcrumb(category="http", data={"url": breadcrumb_url})
    span_url = request.POST.get("span_url")
    if span_url:
        with sentry_sdk.start_span(op="http.client", name="credential-url") as span:
            span.set_data("url", span_url)
    message = f"credential escaped as {leaked_alias}"
    raise RuntimeError(message)


def _asset_referrer(request: HttpRequest) -> HttpResponse:
    return HttpResponse(request.META.get("HTTP_REFERER", ""))


urlpatterns = [
    path(
        "convites/aceitar/<str:credential>/",
        _credential_form,
        name="invite-accept",
    ),
    path(
        "accounts/confirm-email/<str:key>/",
        _credential_exception,
        name="account_confirm_email",
    ),
    path("assets/credential.css", _asset_referrer, name="credential-asset"),
]


class _CaptureTransport(Transport):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[Event] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        for item in envelope.items:
            event = item.get_event()
            if event is None:
                event = item.get_transaction_event()
            if event is not None:
                self.events.append(event)


def _csrf_submission() -> tuple[Client, str, dict[str, str]]:
    client = Client(enforce_csrf_checks=True)
    credential_path = f"/convites/aceitar/{REFERRER_CANARY}/"
    csrf_page = client.get(credential_path, secure=True)
    form = {"csrfmiddlewaretoken": csrf_page.content.decode()}
    return client, credential_path, form


def _gunicorn_access_log_format() -> str:
    compose = PROD_COMPOSE.read_text()
    match = re.search(
        r"--access-logformat\s+(?P<quote>['\"])(?P<format>.*?)(?P=quote)",
        compose,
    )
    assert match is not None, "production Gunicorn has no explicit access-logformat"
    return match.group("format")


def _first_sentry_request_body() -> dict[str, str]:
    return {
        "password1": SENTRY_PASSWORD1_CANARY,
        "password2": SENTRY_PASSWORD2_CANARY,
        "oldpassword": SENTRY_OLDPASSWORD_CANARY,
        "code": SENTRY_TOTP_CODE_CANARY,
        "transaction_url": (
            f"http://localhost/accounts/confirm-email/{SENTRY_TRANSACTION_CANARY}/"
        ),
        "breadcrumb_url": (
            f"http://localhost/accounts/confirm-email/{SENTRY_BREADCRUMB_CANARY}/"
        ),
        "span_url": (f"http://localhost/accounts/confirm-email/{SENTRY_SPAN_CANARY}/"),
    }


def _capture_real_exception_events(
    live_server: LiveServer,
    *,
    hardened: bool,
) -> tuple[list[Event], tuple[int, int]]:
    transport = _CaptureTransport()
    options = {
        **prod_settings.SENTRY_OPTIONS,
        "dsn": SENTRY_TEST_DSN,
        "transport": transport,
        "traces_sample_rate": 1.0,
    }
    if not hardened:
        options.update(
            {
                "event_scrubber": EventScrubber(),
                "before_send": None,
                "before_send_transaction": None,
            }
        )

    previous_client = sentry_sdk.get_client()
    client = sentry_sdk.Client(**options)
    sentry_sdk.get_global_scope().set_client(client)
    server_url = urlsplit(live_server.url)
    assert server_url.hostname is not None
    connection = http_client.HTTPConnection(
        server_url.hostname,
        server_url.port,
        timeout=5,
    )
    try:
        connection.request(
            "POST",
            f"/accounts/confirm-email/{SENTRY_REAL_URL_CANARY}/",
            body=urlencode(_first_sentry_request_body()),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": (
                    f"http://localhost/accounts/confirm-email/{SENTRY_REFERER_CANARY}/"
                ),
            },
        )
        response = connection.getresponse()
        response.read()
        connection.request(
            "POST",
            f"/accounts/confirm-email/{SENTRY_RECOVERY_PATH_CANARY}/",
            body=urlencode({"code": SENTRY_RECOVERY_CODE_CANARY}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        recovery_response = connection.getresponse()
        recovery_response.read()
        client.flush()
    finally:
        connection.close()
        client.close()
        sentry_sdk.get_global_scope().set_client(previous_client)
    return transport.events, (response.status, recovery_response.status)


def test_gunicorn_access_log_retains_forensics_without_credential_urls() -> None:
    access_format = _gunicorn_access_log_format()

    assert set(re.findall(r"%\([^)]*\)s", access_format)) >= REQUIRED_GUNICORN_ATOMS
    assert FORBIDDEN_GUNICORN_ATOMS.isdisjoint(access_format)
    assert "referer" not in access_format.lower()


def test_sentry_before_send_scrubs_a_constructed_credential_url() -> None:
    event: dict[str, Any] = {
        "request": {
            "url": f"https://testserver/convites/aceitar/{SENTRY_URL_CANARY}/",
        },
    }
    options = getattr(prod_settings, "SENTRY_OPTIONS", {})
    before_send = options.get("before_send", lambda original, _hint: original)

    scrubbed = before_send(event, {})

    assert scrubbed is not None
    assert SENTRY_URL_CANARY not in json.dumps(scrubbed)


def test_sentry_event_scrubber_extends_the_sdk_denylist_recursively() -> None:
    scrubber = prod_settings.SENTRY_OPTIONS["event_scrubber"]
    expected_fields = {
        "code",
        "confirmation_code",
        "key",
        "login_code",
        "oldpassword",
        "password1",
        "password2",
        "recovery-code",
        "recovery_code",
    }

    assert isinstance(scrubber, EventScrubber)
    assert set(scrubber.denylist) >= expected_fields
    assert scrubber.recursive is True


@override_settings(
    ROOT_URLCONF=__name__,
    MIDDLEWARE=["django.middleware.security.SecurityMiddleware"],
)
@pytest.mark.django_db(transaction=True)
def test_real_integration_control_exposes_every_planted_canary(
    live_server: LiveServer,
) -> None:
    events, statuses = _capture_real_exception_events(live_server, hardened=False)
    error_events = [event for event in events if event.get("exception")]
    transaction_events = [
        event for event in events if event.get("type") == "transaction"
    ]
    first_error = next(
        event
        for event in error_events
        if SENTRY_REAL_URL_CANARY in json.dumps(event.get("request"))
    )
    recovery_error = next(
        event
        for event in error_events
        if SENTRY_RECOVERY_CODE_CANARY in json.dumps(event.get("request"))
    )
    request = cast("dict[str, Any]", first_error["request"])
    recovery_request = cast("dict[str, Any]", recovery_error["request"])
    exception = cast("dict[str, Any]", first_error["exception"])
    assert statuses == (
        HTTPStatus.INTERNAL_SERVER_ERROR,
        HTTPStatus.INTERNAL_SERVER_ERROR,
    )
    assert len(error_events) >= 2
    assert len(transaction_events) >= 2
    assert SENTRY_REAL_URL_CANARY in request["url"]
    assert SENTRY_REFERER_CANARY in request["headers"]["Referer"]
    assert request["data"]["password1"] == SENTRY_PASSWORD1_CANARY
    assert request["data"]["password2"] == SENTRY_PASSWORD2_CANARY
    assert request["data"]["oldpassword"] == SENTRY_OLDPASSWORD_CANARY
    assert request["data"]["code"] == SENTRY_TOTP_CODE_CANARY
    assert recovery_request["data"]["code"] == SENTRY_RECOVERY_CODE_CANARY
    assert SENTRY_TRANSACTION_CANARY in json.dumps(
        [event.get("transaction") for event in transaction_events]
    )
    assert SENTRY_BREADCRUMB_CANARY in json.dumps(first_error["breadcrumbs"])
    assert SENTRY_SPAN_CANARY in json.dumps(
        [event.get("spans") for event in transaction_events]
    )
    frame_variables = [
        frame.get("vars")
        for value in exception["values"]
        for frame in value["stacktrace"]["frames"]
    ]
    assert SENTRY_REAL_URL_CANARY in json.dumps(frame_variables)


@override_settings(
    ROOT_URLCONF=__name__,
    MIDDLEWARE=["django.middleware.security.SecurityMiddleware"],
)
@pytest.mark.django_db(transaction=True)
def test_sentry_scrubs_real_exception_events_and_transactions(
    live_server: LiveServer,
) -> None:
    events, statuses = _capture_real_exception_events(live_server, hardened=True)
    error_events = [event for event in events if event.get("exception")]
    transaction_events = [
        event for event in events if event.get("type") == "transaction"
    ]
    serialized = json.dumps(events)
    assert statuses == (
        HTTPStatus.INTERNAL_SERVER_ERROR,
        HTTPStatus.INTERNAL_SERVER_ERROR,
    )
    assert len(error_events) >= 2
    assert len(transaction_events) >= 2
    assert SENTRY_REAL_URL_CANARY not in serialized
    assert SENTRY_REFERER_CANARY not in serialized
    assert SENTRY_PASSWORD1_CANARY not in serialized
    assert SENTRY_PASSWORD2_CANARY not in serialized
    assert SENTRY_OLDPASSWORD_CANARY not in serialized
    assert SENTRY_TOTP_CODE_CANARY not in serialized
    assert SENTRY_TRANSACTION_CANARY not in serialized
    assert SENTRY_BREADCRUMB_CANARY not in serialized
    assert SENTRY_SPAN_CANARY not in serialized
    assert SENTRY_RECOVERY_PATH_CANARY not in serialized
    assert SENTRY_RECOVERY_CODE_CANARY not in serialized
    assert prod_settings.SENTRY_REDACTION_MARKER in serialized


def test_referrer_policy_is_strict_origin_globally() -> None:
    assert base_settings.SECURE_REFERRER_POLICY == "strict-origin"
    assert prod_settings.SECURE_REFERRER_POLICY == "strict-origin"


@override_settings(
    ROOT_URLCONF=__name__,
    MIDDLEWARE=SECURITY_TEST_MIDDLEWARE,
)
@pytest.mark.django_db
def test_asset_request_from_credential_page_sends_only_the_origin() -> None:
    client = Client(enforce_csrf_checks=True)
    page = client.get(f"/convites/aceitar/{REFERRER_CANARY}/", secure=True)

    assert page.status_code == HTTPStatus.OK
    assert page.headers["Referrer-Policy"] == "strict-origin"

    origin = "https://testserver/"
    asset = client.get(
        "/assets/credential.css",
        secure=True,
        headers={"referer": origin},
    )

    assert asset.content.decode() == origin
    assert REFERRER_CANARY not in asset.content.decode()


@override_settings(
    ROOT_URLCONF=__name__,
    MIDDLEWARE=SECURITY_TEST_MIDDLEWARE,
)
@pytest.mark.django_db
def test_https_post_succeeds_through_the_origin_branch() -> None:
    client, credential_path, form = _csrf_submission()

    response = client.post(
        credential_path,
        form,
        secure=True,
        headers={
            "origin": "https://testserver",
            "referer": "https://foreign.example/credential/",
        },
    )

    assert response.status_code == HTTPStatus.OK


@override_settings(
    ROOT_URLCONF=__name__,
    MIDDLEWARE=SECURITY_TEST_MIDDLEWARE,
)
@pytest.mark.django_db
def test_https_post_succeeds_through_the_strict_referer_fallback() -> None:
    client, credential_path, form = _csrf_submission()

    response = client.post(
        credential_path,
        form,
        secure=True,
        headers={"referer": "https://testserver/"},
    )

    assert response.status_code == HTTPStatus.OK


@override_settings(
    ROOT_URLCONF=__name__,
    MIDDLEWARE=SECURITY_TEST_MIDDLEWARE,
)
@pytest.mark.django_db
def test_https_post_rejects_a_foreign_referer_without_an_origin() -> None:
    client, credential_path, form = _csrf_submission()

    response = client.post(
        credential_path,
        form,
        secure=True,
        headers={"referer": "https://foreign.example/credential/"},
    )

    assert response.status_code == HTTPStatus.FORBIDDEN
