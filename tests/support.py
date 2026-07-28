"""Helpers shared across suites, kept out of any one feature's test module."""

from allauth.mfa.totp.internal.auth import TOTP

from apps.accounts.models import User

TOTP_SECRET = "MFRGGZDFMZTWQ2LKNNWG23TPOA"  # noqa: S105


def enrol_totp(user: User) -> None:
    """Give a firm-side account the second factor the product requires of it.

    Anyone holding an active `Membership` — or `is_staff` — is diverted to enrolment
    before reaching any view, so a fixture that builds a firm-side user and skips this
    is exercising that redirect rather than whatever it meant to assert.
    """
    TOTP.activate(user, TOTP_SECRET)
