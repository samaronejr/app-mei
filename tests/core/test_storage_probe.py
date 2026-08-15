"""Verify the private-storage probe by effect without contacting a real bucket."""

import hashlib
import importlib
from dataclasses import dataclass
from email.message import Message
from io import StringIO
from pathlib import Path
from typing import NoReturn
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import Request

import pytest
from django.core.files.storage import FileSystemStorage
from django.core.management import call_command
from pytest_django.fixtures import DjangoAssertNumQueries

PROBE_MODULE = "apps.core.management.commands.storage_probe"
PROBE_SUFFIX = "test-probe-suffix"
PROBE_KEY = f"_probe/{PROBE_SUFFIX}"
CREDENTIAL_CANARIES = (
    "PILOT203_ACCESS_KEY_CANARY",
    "PILOT203_SECRET_KEY_CANARY",
)


@dataclass(frozen=True, slots=True)
class ProbeHarness:
    """Filesystem-backed probe dependencies and their observable calls."""

    storage: FileSystemStorage
    anonymous_urls: list[str]
    random_bytes: Mock
    token_urlsafe: Mock


def _harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ProbeHarness:
    """Replace storage/network boundaries while preserving the command interface."""
    probe_module = importlib.import_module(PROBE_MODULE)
    storage = FileSystemStorage(
        location=tmp_path,
        base_url="https://public-storage.invalid/media/",
    )
    storage.__dict__.update(
        endpoint_url="https://object-storage.invalid",
        bucket_name="private-bucket",
        access_key=CREDENTIAL_CANARIES[0],
        secret_key=CREDENTIAL_CANARIES[1],
    )
    monkeypatch.setattr(probe_module, "default_storage", storage)

    anonymous_urls: list[str] = []

    def refuse_anonymous(request: Request, *, timeout: float) -> NoReturn:
        anonymous_urls.append(request.full_url)
        assert timeout == 10
        raise HTTPError(request.full_url, 404, "not found", hdrs=Message(), fp=None)

    monkeypatch.setattr(probe_module, "urlopen", refuse_anonymous)
    random_bytes = Mock(return_value=b"p" * 1024)
    token_urlsafe = Mock(return_value=PROBE_SUFFIX)
    monkeypatch.setattr(probe_module.os, "urandom", random_bytes)
    monkeypatch.setattr(probe_module.secrets, "token_urlsafe", token_urlsafe)
    return ProbeHarness(storage, anonymous_urls, random_bytes, token_urlsafe)


@pytest.mark.django_db
def test_probe_verifies_storage_without_database_or_secret_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    django_assert_num_queries: DjangoAssertNumQueries,
) -> None:
    """The happy path proves both authenticated access and anonymous refusal."""
    harness = _harness(monkeypatch, tmp_path)
    stdout = StringIO()

    with django_assert_num_queries(0):
        call_command("storage_probe", stdout=stdout)

    harness.random_bytes.assert_called_once_with(1024)
    harness.token_urlsafe.assert_called_once_with(16)
    assert harness.anonymous_urls == [
        "https://object-storage.invalid/private-bucket/_probe/test-probe-suffix",
        "https://public-storage.invalid/media/_probe/test-probe-suffix",
    ]
    assert not harness.storage.exists(PROBE_KEY)

    output = stdout.getvalue()
    assert output.splitlines() == [
        f"KEY_SUFFIX_SHA256 {hashlib.sha256(PROBE_SUFFIX.encode()).hexdigest()}",
        "PASS write",
        "PASS authenticated-read",
        (
            "PASS anonymous-direct-refusal: paired with authenticated-read PASS "
            "for the same key"
        ),
        "PASS storage-url-anonymous-refusal",
        "PASS delete-and-confirm-gone",
    ]
    assert PROBE_SUFFIX not in output
    assert not any(canary in output for canary in CREDENTIAL_CANARIES)
    assert "http" not in output.lower()
    assert all(
        line.startswith(("KEY_SUFFIX_SHA256 ", "PASS ", "FAIL "))
        for line in output.splitlines()
    )


def test_read_failure_exits_nonzero_names_step_and_still_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed authenticated read cannot strand its newly written probe object."""
    harness = _harness(monkeypatch, tmp_path)

    def fail_read(_name: str, _mode: str = "rb") -> NoReturn:
        raise OSError

    monkeypatch.setattr(harness.storage, "open", fail_read)
    stdout = StringIO()

    with pytest.raises(SystemExit) as raised:
        call_command("storage_probe", stdout=stdout)

    assert raised.value.code == 1
    assert stdout.getvalue().splitlines()[-1] == "FAIL step=authenticated-read"
    assert not harness.storage.exists(PROBE_KEY)
