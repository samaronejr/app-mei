"""Querysets that answer portfolio questions without leaving the tenant."""

from typing import TYPE_CHECKING

from django.db import models

from apps.core.managers import TenantScopedManager
from apps.fiscal.validators import CNPJ_ALPHABET, normalize_document

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

    def search(self, query: str) -> models.QuerySet["ClientCompany"]:
        """Find clients by name or by document, however the document was punctuated.

        The documents are stored normalized, so a user pasting `12.ABC.345/01DE-35`
        out of an email would match nothing at all under a plain `icontains`. The query
        is normalized the same way the column was before it is compared.

        The document clause is added only when the normalized query is non-empty and
        consists entirely of permitted document characters. Without that guard a search
        for `.` normalizes to the empty string and `cnpj__contains=""` matches the
        whole registry — an accidental full export triggered from a search box.
        """
        stripped = query.strip()
        if not stripped:
            return self.get_queryset().none()

        matches = models.Q(legal_name__icontains=stripped) | models.Q(
            trade_name__icontains=stripped,
        )

        normalized = normalize_document(stripped)
        if normalized and all(char in CNPJ_ALPHABET for char in normalized):
            matches |= models.Q(cnpj__contains=normalized)
            matches |= models.Q(cpf__contains=normalized)

        return self.get_queryset().filter(matches).distinct()
