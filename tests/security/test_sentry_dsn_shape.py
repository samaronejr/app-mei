from __future__ import annotations

import importlib

import pytest
import sentry_sdk
from django.core.exceptions import ImproperlyConfigured

from config.settings import prod as prod_settings

VALID_DSN = "https://0123456789abcdef0123456789abcdef@o0.ingest.sentry.io/0"


def _reload_prod(monkeypatch: pytest.MonkeyPatch, dsn: str) -> None:
    monkeypatch.setenv("SENTRY_DSN", dsn)
    importlib.reload(prod_settings)


def test_malformed_dsn_is_rejected_without_leaking_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ImproperlyConfigured) as error:
        _reload_prod(monkeypatch, "CHANGEME")

    assert "SENTRY_DSN is set but is not a DSN URL" in str(error.value)
    assert "CHANGEME" not in str(error.value)


def test_valid_dsn_activates_sentry(monkeypatch: pytest.MonkeyPatch) -> None:
    _reload_prod(monkeypatch, VALID_DSN)
    assert sentry_sdk.get_client().is_active()


def test_00_empty_dsn_disables_sentry(monkeypatch: pytest.MonkeyPatch) -> None:
    _reload_prod(monkeypatch, "")
    sentry_sdk.get_global_scope().set_client(None)
    assert not sentry_sdk.get_client().is_active()
