"""Inject the clock at the read-model boundary, not the process-wide Django clock."""

import pytest

from tests.briefing.fixtures import NOW


@pytest.fixture(autouse=True)
def briefing_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("apps.briefing.queries.clock", lambda: NOW)
