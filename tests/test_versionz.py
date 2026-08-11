"""The /versionz probe exposes only the immutable image release identity."""

import importlib
from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path
from typing import Final, cast

import pytest
from django.conf import settings
from django.db import connections
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.core import release

RELEASE_SHA: Final = "a" * 40
WRONG_RELEASE_SHA: Final = "b" * 40
URLCONF_HOSTS: Final[tuple[tuple[str, str], ...]] = (
    ("config.urls", "testserver"),
    ("config.urls", "alpha.localhost"),
    ("apps.portal.urls", "alpha-portal.localhost"),
)
RELEASE_CASES: Final = [
    pytest.param((RELEASE_SHA, RELEASE_SHA), id="known"),
    pytest.param((None, "unknown"), id="missing"),
    pytest.param(("not-a-release", "unknown"), id="invalid"),
]


@pytest.fixture(autouse=True)
def _allowed_hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]


@pytest.fixture
def release_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    original_path = release.RELEASE_PATH
    path = tmp_path / "RELEASE"
    monkeypatch.setattr(release, "RELEASE_PATH", path)
    release.current_release.cache_clear()
    try:
        yield path
    finally:
        monkeypatch.setattr(release, "RELEASE_PATH", original_path)
        release.current_release.cache_clear()
        release.current_release()


def _prime_release(path: Path, stored_release: str | None) -> str:
    path.unlink(missing_ok=True)
    if stored_release is not None:
        path.write_text(stored_release, encoding="utf-8")
    release.current_release.cache_clear()
    return release.current_release()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("release_case", RELEASE_CASES)
@pytest.mark.parametrize(("urlconf", "host"), URLCONF_HOSTS)
def test_versionz_exposes_only_the_import_time_release_on_every_host(
    client: Client,
    release_path: Path,
    release_case: tuple[str | None, str],
    urlconf: str,
    host: str,
) -> None:
    # Given the release file state that existed when the process imported its reader
    stored_release, expected_release = release_case
    imported_release = _prime_release(release_path, stored_release)
    release_path.write_text(WRONG_RELEASE_SHA, encoding="utf-8")

    # When a monitor polls either host's URL tree after the file changes
    with CaptureQueriesContext(connections["default"]) as captured:
        response = client.get(
            reverse("versionz", urlconf=urlconf),
            headers={"host": host},
        )

    # Then only the safe import-time value is returned, uncached and transaction-free.
    statements = [query["sql"] for query in captured.captured_queries]
    assert imported_release == expected_release
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"release": expected_release}
    assert response.headers["Cache-Control"] == "no-store"
    assert statements == []
    assert "/versionz" in settings.ACCESS_LOG_EXEMPT_PREFIXES


@pytest.mark.django_db(transaction=True)
def test_versionz_reports_unknown_when_the_dev_bind_mount_hides_release(
    client: Client,
) -> None:
    # Given the normal development shape, where /app/RELEASE is absent
    release.current_release.cache_clear()

    # When the platform-host probe is requested
    response = client.get(reverse("versionz"))

    # Then absence is disclosed only as the fixed safe fallback.
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"release": "unknown"}


@pytest.mark.parametrize(
    ("stored_release", "expected_release"),
    [
        pytest.param(RELEASE_SHA, RELEASE_SHA, id="known"),
        pytest.param(None, None, id="unknown"),
    ],
)
def test_sentry_options_include_only_a_known_release(
    release_path: Path,
    stored_release: str | None,
    expected_release: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imported_release = _prime_release(release_path, stored_release)
    monkeypatch.setenv("SENTRY_DSN", "")

    prod_settings = importlib.import_module("config.settings.prod")
    prod_settings = importlib.reload(prod_settings)
    sentry_options = cast("dict[str, object]", vars(prod_settings)["SENTRY_OPTIONS"])

    if expected_release is None:
        assert imported_release == "unknown"
        assert "release" not in sentry_options
    else:
        assert imported_release == expected_release
        assert sentry_options["release"] == expected_release
