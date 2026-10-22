"""AXRM-005: the scanner status job -- PING + VERSION to clamd, shown, never readiness.

`ping_scanner` (beat, platform-wide) asks clamd `PING` then `VERSION` over the same
adapter socket the scan uses and records the outcome on the `scanner` heartbeat:
last attempt, last success, the signature database version and an error code. The
Automações page shows it. `/readyz` never asks the scanner anything. The clamd here
is `tests.fakes.clamd`; time is injected.
"""

from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Final

import pytest
from django.conf import settings as project_settings
from django.test import Client
from pytest_django.fixtures import SettingsWrapper

from apps.cycles.scanner_status import (
    DISABLED_CODE,
    SCANNER_HEARTBEAT,
    UNAVAILABLE_CODE,
    check_scanner,
)
from apps.cycles.scanning import ClamdScanner
from apps.cycles.tasks import ping_scanner
from apps.delivery.jobs import JOB_LABELS, registered_jobs
from apps.obligations.models import SchedulerHeartbeat
from tests.fakes.clamd import ENGINE, SIGNATURE_VERSION, FakeClamd, fake_clamd
from tests.portal.request_fixtures import portal_setup

pytestmark = pytest.mark.django_db(transaction=True)

NOW: Final = datetime(2026, 10, 22, 12, tzinfo=UTC)
ENTRY: Final = "ping-scanner"


def _clamd(server: FakeClamd, timeout: float = 2) -> ClamdScanner:
    return ClamdScanner("127.0.0.1", server.port, timeout=timeout)


def _stamp() -> SchedulerHeartbeat:
    return SchedulerHeartbeat.objects.get(name=SCANNER_HEARTBEAT)


def test_a_healthy_clamd_records_success_and_its_signature_version() -> None:
    with fake_clamd() as server:
        assert check_scanner(_clamd(server), now=NOW) == "ok"
        commands = list(server.commands)

    assert commands == [b"zPING\0", b"zVERSION\0"]
    stamp = _stamp()
    assert stamp.last_attempt_at == NOW
    assert stamp.last_success_at == NOW
    assert stamp.last_error_code == ""
    assert stamp.outcomes_json == {
        "engine": ENGINE,
        "signature_version": SIGNATURE_VERSION,
    }


def test_a_clamd_that_never_answers_fails_closed_and_keeps_the_last_success() -> None:
    with fake_clamd() as server:
        check_scanner(_clamd(server), now=NOW)
    later = NOW + timedelta(minutes=5)

    with fake_clamd(silent=True) as hung:
        assert check_scanner(_clamd(hung, timeout=0.2), now=later) == UNAVAILABLE_CODE

    stamp = _stamp()
    assert stamp.last_attempt_at == later
    assert stamp.last_success_at == NOW
    assert stamp.last_error_code == UNAVAILABLE_CODE
    assert stamp.outcomes_json["signature_version"] == SIGNATURE_VERSION


@pytest.mark.parametrize("ping_reply", [b"PANG", b""])
def test_a_wrong_ping_answer_is_unavailable(ping_reply: bytes) -> None:
    with fake_clamd(ping_reply=ping_reply) as server:
        assert check_scanner(_clamd(server), now=NOW) == UNAVAILABLE_CODE
        assert server.commands == [b"zPING\0"]
    assert _stamp().last_success_at is None


def test_a_refused_connection_is_unavailable() -> None:
    server = FakeClamd()
    port = server.port
    server.close()
    scanner = ClamdScanner("127.0.0.1", port, timeout=1)
    assert check_scanner(scanner, now=NOW) == UNAVAILABLE_CODE
    assert _stamp().last_error_code == UNAVAILABLE_CODE


def test_scanning_switched_off_is_recorded_as_off_not_as_success() -> None:
    assert check_scanner(None, now=NOW) == DISABLED_CODE
    stamp = _stamp()
    assert stamp.last_attempt_at == NOW
    assert stamp.last_success_at is None
    assert stamp.last_error_code == DISABLED_CODE


def test_the_beat_task_reads_the_clamd_settings(settings: SettingsWrapper) -> None:
    settings.CLAMAV_ENABLED = False
    assert ping_scanner.apply().get() == DISABLED_CODE
    with fake_clamd() as server:
        settings.CLAMAV_ENABLED = True
        settings.CLAMD_HOST = "127.0.0.1"
        settings.CLAMD_PORT = server.port
        assert ping_scanner.apply().get() == "ok"
        assert server.commands == [b"zPING\0", b"zVERSION\0"]
    assert _stamp().outcomes_json["signature_version"] == SIGNATURE_VERSION


def test_the_task_is_on_the_beat_schedule_with_a_label() -> None:
    entry = project_settings.CELERY_BEAT_SCHEDULE[ENTRY]
    assert entry["task"] == "apps.cycles.tasks.ping_scanner"
    assert entry["schedule"] == 300.0
    assert JOB_LABELS["ping_scanner"] == "Verificador antivírus"


def test_registered_jobs_carry_the_scanner_status(settings: SettingsWrapper) -> None:
    settings.CELERY_BEAT_SCHEDULE = {
        ENTRY: project_settings.CELERY_BEAT_SCHEDULE[ENTRY]
    }
    with fake_clamd() as server:
        check_scanner(_clamd(server), now=NOW)

    [job] = registered_jobs()

    assert job.name == ENTRY
    assert job.label == "Verificador antivírus"
    assert job.last_completed_at == NOW
    assert job.last_attempt_at == NOW
    assert job.signature_version == SIGNATURE_VERSION
    assert job.last_error_code == ""


def test_the_automations_page_shows_the_scanner_and_its_failure(
    settings: SettingsWrapper, tmp_path: Path
) -> None:
    settings.MEDIA_ROOT = tmp_path
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    setup = portal_setup("axrm005-jobs")
    with fake_clamd() as server:
        check_scanner(_clamd(server), now=NOW)
    with fake_clamd(silent=True) as hung:
        check_scanner(_clamd(hung, timeout=0.2), now=NOW + timedelta(minutes=5))

    page = setup.firm().get("/gestao/automacoes/")

    assert page.status_code == HTTPStatus.OK
    body = page.content.decode()
    article = body[body.index(f'data-job="{ENTRY}"') :]
    article = article[: article.index("</article>")]
    assert "Verificador antivírus" in article
    assert f"Versão das assinaturas: {SIGNATURE_VERSION}" in article
    assert f'data-job-error="{UNAVAILABLE_CODE}"' in article
    assert "O verificador antivírus não respondeu" in article


def test_readyz_never_asks_the_scanner(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", "localhost"]
    with fake_clamd(silent=True) as hung:
        settings.CLAMAV_ENABLED = True
        settings.CLAMD_HOST = "127.0.0.1"
        settings.CLAMD_PORT = hung.port
        check_scanner(None, now=NOW)
        SchedulerHeartbeat.objects.filter(name=SCANNER_HEARTBEAT).update(
            last_error_code=UNAVAILABLE_CODE
        )

        response = Client().get("/readyz")

        assert hung.commands == []
    payload = response.json()
    assert set(payload) <= {"status", "database", "redis", "reason"}
    assert "scanner" not in response.content.decode()
