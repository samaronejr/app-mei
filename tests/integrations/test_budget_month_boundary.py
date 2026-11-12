"""Qualification budgets follow UTC while signed termo dates remain local."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from django.core.files.storage import default_storage
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.integrations.budget_models import ProviderBudget
from apps.integrations.serpro.models import FirmProviderReservation
from apps.integrations.serpro.qualification import qualify_termo, synthetic_termo
from apps.integrations.serpro.termo import read_signed_termo
from tests.fakes.serpro import FakeSerpro
from tests.integrations.test_serpro_termo import accepted, submit

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(params=["2026-10-31T22:30:00-03:00", "2026-11-01T00:30:00-03:00"])
def instant(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> datetime:
    moment = datetime.fromisoformat(request.param).astimezone(UTC)
    monkeypatch.setattr("django.utils.timezone.now", lambda: moment)
    monkeypatch.setattr("time.time", moment.timestamp)
    return moment


@pytest.fixture
def qualification_settings(settings: SettingsWrapper, tmp_path: Path) -> None:
    settings.MEDIA_ROOT = tmp_path
    settings.PROVIDERS_PAUSED = False
    settings.SERPRO_PAUSED = False
    settings.SERPRO_ENVIRONMENT = "fake"
    settings.PROVIDER_GLOBAL_MONTHLY_CAP_BRL = {"serpro": "0"}


@pytest.mark.usefixtures("qualification_settings")
def test_synthetic_termo_reserves_the_utc_budget_with_local_signed_dates(
    instant: datetime,
) -> None:
    world = synthetic_termo("fake")
    with default_storage.open(world.document.storage_key, "rb") as stored:
        signed = read_signed_termo(stored.read(), world.parties)
    assert signed.signed_on == timezone.localdate()
    assert signed.valid_until == timezone.localdate() + timedelta(days=365)
    with FakeSerpro((accepted(),)) as peer:
        assert submit(world, peer.origin) == "sucesso"
        assert len(peer.requests) == 1
    with tenant_context(world.tenant.pk):
        held = FirmProviderReservation.objects.select_related("budget").get()
        assert held.budget.window_start == instant.date().replace(day=1)
        assert held.state == "released"
        assert ProviderBudget.objects.get().reserved == 0


@pytest.mark.usefixtures("qualification_settings")
def test_operator_qualification_succeeds_across_the_brt_month_boundary(
    instant: datetime,
) -> None:
    result = qualify_termo("fake")
    assert instant.date() == date(2026, 11, 1)
    assert result.http_status == 200
    assert result.parsed_fields["accepted"] is True
    assert result.parsed_fields["authorization_status"] == "aceito"
