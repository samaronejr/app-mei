from pathlib import Path

import pytest
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.tenants.models import Tenant
from tests.portal.request_fixtures import PortalSetup, portal_setup


@pytest.fixture
def setup(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> PortalSetup:
    instant = timezone.now()
    monkeypatch.setattr(timezone, "now", lambda: instant)
    settings.MEDIA_ROOT = tmp_path
    settings.AI_PROVIDER = "fake"
    settings.AI_GLOBAL_DISABLED = False
    settings.AI_SHADOW_TENANT_DAILY_CALLS = 100
    settings.AI_SHADOW_TENANT_DAILY_BRL = "5.00"
    graph = portal_setup("t24-shadow")
    Tenant.objects.filter(pk=graph.graph.tenant.pk).update(
        features=["cycles_r1", "ai_shadow"]
    )
    return graph
