"""The user fence shared by roster writers and exact-effect decisions."""

from collections.abc import Iterable
from uuid import UUID

from apps.accounts.models import User


def lock_users(user_ids: Iterable[UUID]) -> dict[UUID, User]:
    """Read fresh users before any member tuple or tenant policy is locked.

    NO KEY UPDATE conflicts with activity updates but remains compatible with
    audit and membership foreign-key checks. Callers own the transaction.
    """
    return {
        user.pk: user
        for user in User.objects.select_for_update(no_key=True)
        .filter(pk__in=set(user_ids))
        .order_by("pk")
    }
