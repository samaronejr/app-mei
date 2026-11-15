"""The registered capture scenarios execute the real synchronizer against fakes."""

from pathlib import Path

import pytest
from pytest_django.fixtures import SettingsWrapper

from apps.integrations.serpro.qualification import SCENARIOS
from tests.integrations.capture_fixtures import capture_world

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("collection", ["nfe_recebidas", "nfsen_recebidas"])
def test_registered_capture_scenario_produces_a_minimized_fake_receipt(
    settings: SettingsWrapper,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    collection: str,
) -> None:
    capture_world(settings, tmp_path, monkeypatch)
    scenario = SCENARIOS.get(("focus", collection))
    assert scenario is not None
    receipt = scenario.run("fake")
    assert receipt.http_status == 200
    assert receipt.billing_class == "incluido_no_plano"
    assert receipt.parsed_fields == {
        "collection": collection,
        "environment": "fake",
        "documents": 1,
        "cursor": "1",
        "complete_scan": collection == "nfsen_recebidas",
    }
    assert len(receipt.request_digest) == len(receipt.response_digest) == 64
    assert "synthetic-focus" not in str(receipt)
