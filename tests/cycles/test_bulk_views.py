"""HTTP contracts for exact confirmation, safe reports and terminal polling."""

import re
from datetime import timedelta
from html import unescape
from http import HTTPStatus

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.bulk import BulkItem, BulkKind, BulkOperation
from apps.cycles.bulk_forms import BulkConfigForm
from apps.cycles.tasks import run_bulk_operation
from apps.delivery.models import AutomationPause
from tests.browser.harness import mfa_session_cookie
from tests.cycles.bulk_fixtures import (
    BulkSetup,
    bulk_setup,
    confirm,
    notice_preview_with_reservations,
    reserve_today,
)
from tests.support import enrol_totp
from tests.ui.factories import make_firm

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def desk(settings: SettingsWrapper) -> tuple[BulkSetup, Client]:
    settings.ALLOWED_HOSTS = ["localhost", "127.0.0.1", ".localhost"]
    setup = bulk_setup("bulk-http")
    enrol_totp(setup.graph.actor)
    host = f"{setup.graph.tenant.slug}.localhost"
    session = Client(SERVER_NAME=host)
    cookie = mfa_session_cookie(setup.graph.actor, f"http://{host}/")
    assert "name" in cookie
    assert "value" in cookie
    session.cookies[cookie["name"]] = cookie["value"]
    return setup, session


def test_bulk_version_options_show_names_without_extra_queries(
    desk: tuple[BulkSetup, Client],
) -> None:
    setup, _ = desk
    definition = setup.graph.definition
    with tenant_context(setup.graph.tenant.pk):
        name = definition.playbook.name
        with CaptureQueriesContext(connection) as queries:
            rendered = str(BulkConfigForm()["playbook_version_id"])
    assert len(queries) == 1
    option = re.search(rf'<option value="{definition.pk}">(.*?)</option>', rendered)
    assert option is not None
    label = unescape(option[1])
    assert name in label
    assert f"v{definition.number}" in label
    assert re.search(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", label) is None


def test_http_preview_exact_count_replay_and_terminal_report(
    desk: tuple[BulkSetup, Client],
) -> None:
    setup, session = desk
    created = session.post(
        "/ciclos/lotes/",
        {
            "kind": BulkKind.OPEN_CYCLES,
            "periodo": "2026-09",
            "playbook_version_id": str(setup.graph.definition.pk),
            "subject_ids": [str(row.pk) for row in setup.engagements],
        },
    )
    assert created.status_code == HTTPStatus.FOUND
    with tenant_context(setup.graph.tenant.pk):
        preview = BulkOperation.objects.get()
    url = f"/ciclos/lotes/{preview.pk}/"
    frozen = session.get(url)
    assert frozen.status_code == HTTPStatus.OK
    assert b"data-bulk-preview" in frozen.content
    payload = {
        "action": "confirm",
        "expected_version": str(preview.version),
        "snapshot_digest": preview.snapshot_digest,
        "confirm_count": "2",
    }
    wrong = session.post(url, payload)
    assert wrong.status_code == HTTPStatus.UNPROCESSABLE_CONTENT
    with tenant_context(setup.graph.tenant.pk):
        assert not BulkItem.objects.exists()
    payload["confirm_count"] = "3"
    assert session.post(url, payload).status_code == HTTPStatus.FOUND
    assert session.post(url, payload).status_code == HTTPStatus.CONFLICT
    active = session.get(f"{url}progresso/", HTTP_HX_REQUEST="true")
    assert active.status_code == HTTPStatus.OK
    assert b'hx-trigger="every 2s"' in active.content
    run_bulk_operation(str(preview.tenant_id), str(preview.pk))
    done = session.get(f"{url}progresso/", HTTP_HX_REQUEST="true")
    assert done.status_code == HTTPStatus.OK
    assert b'data-bulk-state="completed"' in done.content
    assert b'hx-trigger="every 2s"' not in done.content
    report = session.get(f"{url}relatorio.csv")
    assert report.status_code == HTTPStatus.OK
    assert report["Content-Type"].startswith("text/csv")
    assert report["Content-Disposition"].startswith("attachment;")
    assert report.streaming is False


@pytest.mark.parametrize(
    "count",
    ["03", "+3", "3.0", " 3 ", "-1", "", pytest.param("9" * 5000, id="overlong")],
)
def test_http_count_requires_the_exact_decimal_integer(
    desk: tuple[BulkSetup, Client], count: str
) -> None:
    setup, session = desk
    preview = setup.preview()
    response = session.post(
        f"/ciclos/lotes/{preview.pk}/",
        {
            "action": "confirm",
            "expected_version": str(preview.version),
            "snapshot_digest": preview.snapshot_digest,
            "confirm_count": count,
        },
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_CONTENT
    with tenant_context(preview.tenant_id):
        assert not BulkItem.objects.exists()


def test_http_expired_preview_does_not_create_items(
    desk: tuple[BulkSetup, Client], monkeypatch: pytest.MonkeyPatch
) -> None:
    setup, session = desk
    preview = setup.preview()
    monkeypatch.setattr(
        "apps.cycles.services.bulk.timezone.now",
        lambda: preview.valid_until + timedelta(seconds=1),
    )
    response = session.post(
        f"/ciclos/lotes/{preview.pk}/",
        {
            "action": "confirm",
            "expected_version": str(preview.version),
            "snapshot_digest": preview.snapshot_digest,
            "confirm_count": "3",
        },
    )
    assert response.status_code == HTTPStatus.CONFLICT
    with tenant_context(preview.tenant_id):
        assert not BulkItem.objects.exists()


def test_notice_preview_shows_the_queue_remaining_daily_quota(
    settings: SettingsWrapper,
) -> None:
    settings.ALLOWED_HOSTS = ["localhost", "127.0.0.1", ".localhost"]
    settings.DELIVERY_DAILY_CAP = 100
    setup, preview = notice_preview_with_reservations("bulk-http-quota", reserved=97)
    foreign = make_firm("bulk-http-quota-foreign")
    tid = setup.graph.tenant.pk
    host = f"{setup.graph.tenant.slug}.localhost"
    session = Client(SERVER_NAME=host)
    cookie = mfa_session_cookie(setup.graph.actor, f"http://{host}/")
    assert "name" in cookie
    assert "value" in cookie
    session.cookies[cookie["name"]] = cookie["value"]
    quota_reads: list[int] = []

    def shown() -> tuple[str, str, bool]:
        """Reload the queue and the same preview: queue, preview, over-quota note."""
        queue = session.get("/comunicacao/")
        with CaptureQueriesContext(connection) as queries:
            detail = session.get(f"/ciclos/lotes/{preview.pk}/")
        assert queue.status_code == HTTPStatus.OK
        assert detail.status_code == HTTPStatus.OK
        quota_reads.append(
            sum(
                "delivery_automationpause" in query["sql"]
                for query in queries.captured_queries
            )
        )
        page = detail.content.decode()
        queued = re.search(
            r"Reservas disponíveis hoje: (\d+)\.", queue.content.decode()
        )
        previewed = re.search(r"<strong[^>]*data-bulk-quota[^>]*>(\d+)</strong>", page)
        assert queued is not None
        assert previewed is not None
        return queued[1], previewed[1], "data-bulk-quota-short" in page

    assert shown() == ("3", "3", False)
    # Showing the capacity must not reserve any of it.
    with tenant_context(tid):
        assert AutomationPause.objects.get(tenant_id=tid).reserved_today == 97
    # Another firm exhausting its own quota changes nothing for this one.
    reserve_today(foreign.tenant.pk, 100)
    assert shown() == ("3", "3", False)
    # Reloading the same preview after this firm's reservations rise is current.
    reserve_today(tid, 100)
    assert shown() == ("0", "0", True)
    # One read of the firm's reservations per render, none per item.
    assert quota_reads == [1, 1, 1]


def test_http_scope_and_csv_never_expose_another_firms_operation(
    desk: tuple[BulkSetup, Client],
) -> None:
    setup, session = desk
    foreign = bulk_setup("bulk-http-foreign", count=1)
    operation = confirm(foreign.graph.actor, foreign.preview())
    for suffix in ("", "progresso/", "relatorio.csv"):
        response = session.get(f"/ciclos/lotes/{operation.pk}/{suffix}")
        assert response.status_code == HTTPStatus.NOT_FOUND
        assert foreign.graph.client.legal_name.encode() not in response.content
    with tenant_context(setup.graph.tenant.pk):
        assert not BulkOperation.objects.exists()
