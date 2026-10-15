"""Flag-off HTML must match the bytes captured before the T19 dispatch change."""

from pathlib import Path

import pytest

from tests.briefing.legacy_fixtures import legacy_response

pytestmark = pytest.mark.django_db(transaction=True)
REFERENCES = Path(__file__).resolve().parents[1] / "fixtures" / "briefing"


@pytest.mark.parametrize("product_shell", [False, True])
def test_dashboard_without_briefing_is_byte_identical(
    monkeypatch: pytest.MonkeyPatch, product_shell: bool
) -> None:
    before = (
        REFERENCES / ("legacy-r1.html" if product_shell else "legacy-mei.html")
    ).read_bytes()

    after = legacy_response(monkeypatch, product_shell=product_shell)

    assert after == before
