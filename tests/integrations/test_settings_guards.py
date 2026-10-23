"""AXRM-007: provider settings ship closed, and production without approval is refused.

Plan A11: `PROVIDERS_PAUSED` defaults to True like `DELIVERY_PAUSED`; both provider
environments default to the fake; production calls need `PROVIDER_REAL_DATA_APPROVED`;
a missing monthly cap means "refuse billable calls". `integrations.E001` turns a
production environment that is neither approved nor paused into a startup error;
`integrations.E002` refuses an environment name outside the four known ones, because a
misspelt `producao` would otherwise slip past E001 as "not production".
"""

import pytest
from django.conf import settings
from django.core.checks import registry
from django.test import override_settings

from apps.integrations.checks import check_provider_settings


def _ids() -> set[str | None]:
    return {message.id for message in check_provider_settings(None)}


def test_the_shipped_defaults_are_closed() -> None:
    assert settings.PROVIDERS_PAUSED is True
    assert settings.SERPRO_PAUSED is False
    assert settings.FOCUS_PAUSED is False
    assert settings.SERPRO_ENVIRONMENT == "fake"
    assert settings.FOCUS_ENVIRONMENT == "fake"
    assert settings.PROVIDER_REAL_DATA_APPROVED is False
    assert settings.PROVIDER_GLOBAL_MONTHLY_CAP_BRL == {}


def test_the_guard_is_registered_and_quiet_under_the_shipped_settings() -> None:
    assert check_provider_settings in registry.registry.registered_checks
    assert check_provider_settings(None) == []


@pytest.mark.parametrize("setting", ["SERPRO_ENVIRONMENT", "FOCUS_ENVIRONMENT"])
def test_production_unpaused_without_approval_is_an_error(setting: str) -> None:
    with override_settings(
        **{setting: "producao"},
        PROVIDERS_PAUSED=False,
        PROVIDER_REAL_DATA_APPROVED=False,
    ):
        assert "integrations.E001" in _ids()


@pytest.mark.parametrize("setting", ["SERPRO_ENVIRONMENT", "FOCUS_ENVIRONMENT"])
def test_production_is_allowed_while_paused_or_once_approved(setting: str) -> None:
    with override_settings(
        **{setting: "producao"},
        PROVIDERS_PAUSED=True,
        PROVIDER_REAL_DATA_APPROVED=False,
    ):
        assert "integrations.E001" not in _ids()
    with override_settings(
        **{setting: "producao"},
        PROVIDERS_PAUSED=False,
        PROVIDER_REAL_DATA_APPROVED=True,
    ):
        assert "integrations.E001" not in _ids()


@pytest.mark.parametrize("value", ["production", "Producao", "producao ", "real"])
def test_an_unknown_environment_name_is_an_error(value: str) -> None:
    with override_settings(FOCUS_ENVIRONMENT=value):
        assert "integrations.E002" in _ids()


def test_homologacao_unpaused_without_approval_is_not_production() -> None:
    with override_settings(
        SERPRO_ENVIRONMENT="trial",
        FOCUS_ENVIRONMENT="homologacao",
        PROVIDERS_PAUSED=False,
    ):
        assert _ids() == set()
