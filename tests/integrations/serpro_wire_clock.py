"""One aware test clock for captured defaults and both SITFIS qualification paths."""

from collections.abc import Iterator
from datetime import UTC, datetime, tzinfo
from types import SimpleNamespace
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from apps.integrations.call_contracts import CallRequest
from apps.integrations.call_models import ProviderCall
from apps.integrations.serpro import adapter, sitfis, sitfis_qualification
from tests.integrations.sitfis_support import Clock, clock

__all__ = ["clock", "wire_clock"]

_DATES = (
    datetime(2026, 11, 30, 23, 59, 59, tzinfo=UTC),
    datetime(2026, 12, 1, 0, 0, 1, tzinfo=UTC),
    datetime(2076, 12, 2, 12, tzinfo=UTC),
)


@pytest.fixture(params=_DATES, ids=["before-month", "after-month", "future"])
def wire_clock(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> Iterator[Clock]:
    current = clock
    current.current = request.param

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            # Imported Django defaults still use timezone.now's datetime global.
            if tz is None:
                return current.now().replace(tzinfo=None)
            return current.now().astimezone(tz)

    monkeypatch.setattr(timezone, "datetime", FrozenDatetime)
    monkeypatch.setattr(timezone, "now", current.now)
    monkeypatch.setattr(
        adapter, "time", SimpleNamespace(time=lambda: current.now().timestamp())
    )
    original = sitfis.reserve

    def reserve(tenant: UUID, link: UUID, call: CallRequest) -> ProviderCall:
        if call.operation == "SP-SIT-R":
            current.advance(microseconds=1)
        return original(tenant, link, call)

    # Emit in a later controlled instant, not in UUID tie-break order.
    monkeypatch.setattr(sitfis, "reserve", reserve)

    def no_wait(_seconds: float) -> None:
        pytest.fail("a zero-wait qualification must not sleep")

    monkeypatch.setattr(sitfis_qualification, "time", SimpleNamespace(sleep=no_wait))
    with timezone.override(ZoneInfo("America/Sao_Paulo")):
        yield current
