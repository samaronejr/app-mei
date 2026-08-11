import pytest
from allauth.mfa import app_settings
from allauth.mfa.totp.internal import auth as totp_auth

from apps.accounts.models import User
from tests import support

PASSWORD = "correct-horse-battery-staple"  # noqa: S105


@pytest.mark.django_db
def test_sign_in_pins_totp_generation_and_verification_to_one_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = app_settings.TOTP_PERIOD * 60_000_000
    wall_clock = support._PinnedClock(boundary - 0.01)
    monkeypatch.setattr(totp_auth, "time", wall_clock)
    real_totp_code = support.totp_code

    def generate_then_cross_the_boundary() -> str:
        code = real_totp_code()
        wall_clock.advance(0.02)
        return code

    monkeypatch.setattr(support, "totp_code", generate_then_cross_the_boundary)
    user = User.objects.create_user(
        email="totp-boundary@example.com",
        password=PASSWORD,
    )
    support.enrol_totp(user)

    client = support.sign_in(user, PASSWORD, with_mfa=True)

    assert client.session.get("_auth_user_id") == str(user.pk)


@pytest.mark.django_db
def test_sign_in_accepts_the_same_user_in_adjacent_totp_steps() -> None:
    user = User.objects.create_user(
        email="totp-replay@example.com",
        password=PASSWORD,
    )
    support.enrol_totp(user)
    support.sign_in(user, PASSWORD, with_mfa=True)

    with pytest.raises(AssertionError, match="TOTP code was not accepted"):
        support.sign_in(user, PASSWORD, with_mfa=True)

    client = support.sign_in(user, PASSWORD, with_mfa=True, totp_step=1)

    assert client.session.get("_auth_user_id") == str(user.pk)
