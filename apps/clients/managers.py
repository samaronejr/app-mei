"""Querysets that answer portfolio questions without leaving the tenant."""

from typing import TYPE_CHECKING

from django.db import models

from apps.core.managers import TenantScopedManager

if TYPE_CHECKING:
    from apps.accounts.models import User
    from apps.clients.models.company import ClientCompany


class ClientCompanyManager(TenantScopedManager["ClientCompany"]):
    """The firm's registry, plus the assignment lens a staff accountant works through.

    `assigned_to` is deliberately only the assignment filter and carries no role
    logic. Whether a given account is *restricted* to its assignments is an
    authorization question, answered once by `apps.authz.services.can` — a manager
    that quietly widened its result for owners would put a second, untested copy of
    the permission matrix here.
    """

    def assigned_to(self, user: "User") -> models.QuerySet["ClientCompany"]:
        """Return the clients this account is explicitly assigned to."""
        return self.get_queryset().filter(assignments__user=user)
