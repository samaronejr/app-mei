"""`core.E013` refuses incomplete or misaligned production SMTP settings.

Like the object-storage guard, this check validates the selected production backend at
boot rather than making the settings module unimportable. Development and tests keep
their console/locmem backends, so the guard is inert there and never opens a connection.
"""

import pytest
from django.core.checks import run_checks
from pytest_django.fixtures import SettingsWrapper

from apps.core.checks import _email_config_errors

SMTP = "django.core.mail.backends.smtp.EmailBackend"
CONSOLE = "django.core.mail.backends.console.EmailBackend"
LOCMEM = "django.core.mail.backends.locmem.EmailBackend"

COMPLETE: dict[str, object] = {
    "EMAIL_BACKEND": SMTP,
    "EMAIL_HOST": "smtp.example.invalid",
    "EMAIL_PORT": 587,
    "EMAIL_HOST_USER": "smtp-user",
    "EMAIL_HOST_PASSWORD": "smtp-password",
    "EMAIL_USE_TLS": True,
    "EMAIL_USE_SSL": False,
    "EMAIL_TIMEOUT": 10,
    "DEFAULT_FROM_EMAIL": "nao-responda@example.invalid",
}


def _configure_email(
    settings: SettingsWrapper,
    **overrides: object,
) -> None:
    for name, value in (COMPLETE | overrides).items():
        setattr(settings, name, value)


@pytest.mark.parametrize("backend", [CONSOLE, LOCMEM])
def test_the_check_is_silent_on_non_smtp_backends(
    settings: SettingsWrapper,
    backend: str,
) -> None:
    # Given the no-network backends used by development and the test suite
    settings.EMAIL_BACKEND = backend

    # Then the production SMTP guard is inert
    assert _email_config_errors() == []


def test_the_check_is_silent_when_smtp_is_fully_configured(
    settings: SettingsWrapper,
) -> None:
    _configure_email(settings)

    assert _email_config_errors() == []


@pytest.mark.parametrize(
    "missing",
    [
        "EMAIL_HOST",
        "EMAIL_HOST_USER",
        "EMAIL_HOST_PASSWORD",
        "DEFAULT_FROM_EMAIL",
    ],
)
def test_the_check_names_each_required_value_left_empty(
    settings: SettingsWrapper,
    missing: str,
) -> None:
    _configure_email(settings, **{missing: ""})

    errors = _email_config_errors()

    assert len(errors) == 1
    assert errors[0].id == "core.E013"
    assert missing in str(errors[0].msg)


@pytest.mark.parametrize(
    ("use_tls", "use_ssl"),
    [(True, True), (False, False)],
    ids=["both-enabled", "neither-enabled"],
)
def test_the_check_requires_exactly_one_encrypted_transport(
    settings: SettingsWrapper,
    *,
    use_tls: bool,
    use_ssl: bool,
) -> None:
    _configure_email(settings, EMAIL_USE_TLS=use_tls, EMAIL_USE_SSL=use_ssl)

    errors = _email_config_errors()

    assert len(errors) == 1
    assert errors[0].id == "core.E013"
    assert "EMAIL_USE_TLS" in str(errors[0].msg)
    assert "EMAIL_USE_SSL" in str(errors[0].msg)


@pytest.mark.parametrize("timeout", [None, 0, -1], ids=["unset", "zero", "negative"])
def test_the_check_rejects_an_unset_or_non_positive_timeout(
    settings: SettingsWrapper,
    timeout: int | None,
) -> None:
    _configure_email(settings, EMAIL_TIMEOUT=timeout)

    errors = _email_config_errors()

    assert len(errors) == 1
    assert errors[0].id == "core.E013"
    assert "EMAIL_TIMEOUT" in str(errors[0].msg)


def test_the_check_rejects_a_localhost_sender(settings: SettingsWrapper) -> None:
    _configure_email(settings, DEFAULT_FROM_EMAIL="nao-responda@localhost")

    errors = _email_config_errors()

    assert len(errors) == 1
    assert errors[0].id == "core.E013"
    assert "DEFAULT_FROM_EMAIL" in str(errors[0].msg)


def test_the_check_names_every_failing_setting_at_once(
    settings: SettingsWrapper,
) -> None:
    _configure_email(
        settings,
        EMAIL_HOST="",
        EMAIL_HOST_USER="",
        EMAIL_HOST_PASSWORD="",
        DEFAULT_FROM_EMAIL="nao-responda@localhost",
        EMAIL_TIMEOUT=None,
        EMAIL_USE_TLS=False,
        EMAIL_USE_SSL=False,
    )

    errors = _email_config_errors()

    assert len(errors) == 1
    assert errors[0].id == "core.E013"
    for setting_name in (
        "DEFAULT_FROM_EMAIL",
        "EMAIL_HOST",
        "EMAIL_HOST_PASSWORD",
        "EMAIL_HOST_USER",
        "EMAIL_TIMEOUT",
        "EMAIL_USE_SSL",
        "EMAIL_USE_TLS",
    ):
        assert setting_name in str(errors[0].msg)


@pytest.mark.django_db
def test_the_email_guard_is_registered_as_a_system_check(
    settings: SettingsWrapper,
) -> None:
    _configure_email(settings, EMAIL_HOST="")

    errors = [message for message in run_checks() if message.id == "core.E013"]

    assert len(errors) == 1
    assert "EMAIL_HOST" in str(errors[0].msg)
