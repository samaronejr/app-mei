"""Ordinary journal writes do not inspect or lock a marker admission."""

from dataclasses import replace
from pathlib import Path
from typing import NoReturn

import pytest
from django.core.files.storage import default_storage
from django.test import override_settings

from apps.integrations import call_unopened, provider_journal
from apps.integrations.call_contracts import CallResult
from apps.integrations.call_models import CallState
from tests.integrations.axrm016_baseline import journal as baseline_journal
from tests.integrations.sitfis_support import clock, world
from tests.integrations.test_sitfis_unopened import Veto, veto

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["clock", "veto", "world"]


class ForbiddenScopeRead:
    def get(self) -> NoReturn:
        message = "ordinary append read marker scope state"
        raise AssertionError(message)


class ForbiddenScopeLock:
    def __enter__(self) -> NoReturn:
        message = "ordinary append acquired the marker lock"
        raise AssertionError(message)

    def __exit__(self, *_args: object) -> None:
        message = "ordinary append released a marker lock"
        raise AssertionError(message)


@pytest.mark.parametrize("has_result", [False, True])
def test_non_marker_path_and_bytes_match_without_scope_access(
    veto: Veto, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, has_result: bool
) -> None:
    veto.run()
    [dispatch] = veto.dispatches
    record = replace(
        dispatch.receipt,
        operation="SP-DAS",
        result=CallResult(CallState.SUCCESS, 200, "ok", "sim", settled_microbrl=320_000)
        if has_result
        else None,
    )
    paths: list[str] = []
    contents: list[bytes] = []
    for name, append in [
        ("baseline", baseline_journal.append),
        ("current", provider_journal.append),
    ]:
        with override_settings(
            STORAGES={
                "default": {
                    "BACKEND": "django.core.files.storage.FileSystemStorage",
                    "OPTIONS": {"location": str(tmp_path / name)},
                }
            }
        ):
            with call_unopened._issue_unopened_write(dispatch.receipt):
                scope = call_unopened._UNOPENED_WRITES.get()
                assert scope is not None
                with monkeypatch.context() as patch:
                    patch.setattr(scope, "lock", ForbiddenScopeLock())
                    patch.setattr(
                        call_unopened, "_UNOPENED_WRITES", ForbiddenScopeRead()
                    )
                    paths.append(append(record))
                assert scope.grants == {id(dispatch.receipt): dispatch.receipt}
            with default_storage.open(paths[-1], "rb") as stream:
                contents.append(stream.read())
    assert paths == [record.path, record.path]
    assert contents == [record.encode(), record.encode()]
