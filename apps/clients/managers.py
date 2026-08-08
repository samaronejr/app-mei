"""Querysets that answer portfolio questions without leaving the tenant."""

from typing import TYPE_CHECKING

from django.db import models

from apps.core.managers import TenantScopedManager
from apps.fiscal.validators import CNPJ_ALPHABET, normalize_document

if TYPE_CHECKING:
    from apps.accounts.models import User
    from apps.clients.models.company import ClientCompany


def search_filter(query: str) -> models.Q | None:
    """Return the lookup matching `query`, or None when it is not a query at all.

    Split out of `search` so that a screen composing this onto an already-scoped
    queryset — `visible_clients()`, say, which a manager cannot see — reuses the one
    definition instead of rebuilding it. A second copy is how a search box comes to
    match a different set of clients than the export beside it.

    `None` rather than an empty `Q()` for a blank query, and the distinction is the
    whole reason this returns an optional. An empty `Q()` filters nothing, so a caller
    that applied it unconditionally would show the entire registry; `search` answers
    the empty queryset instead, because `cnpj__contains=""` matches every row and
    turning a cleared search box into a full export is worse than either. Neither
    default is right for both callers, so the decision is handed back to them.

    The documents are stored normalized, so a user pasting `12.ABC.345/01DE-35` out of
    an email would match nothing under a plain `icontains`. The query is normalized the
    same way the column was — punctuation stripped and **uppercased**, never reduced to
    digits: CNPJ is alphanumeric from 2026-07-31 under IN RFB nº 2.229/2024, and
    stripping the letters out of `12abc` leaves `12`, which matches unrelated legacy
    numbers.

    The document clause is added only when the normalized query is non-empty and
    consists entirely of permitted document characters. Without that guard a search for
    `.` normalizes to the empty string and `cnpj__contains=""` matches the whole
    registry — an accidental full export triggered from a search box.
    """
    stripped = query.strip()
    if not stripped:
        return None

    matches = models.Q(legal_name__icontains=stripped) | models.Q(
        trade_name__icontains=stripped,
    )

    normalized = normalize_document(stripped)
    if normalized and all(char in CNPJ_ALPHABET for char in normalized):
        matches |= models.Q(cnpj__contains=normalized)
        matches |= models.Q(cpf__contains=normalized)

    return matches


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

        A blank query answers the EMPTY queryset rather than the registry, which is the
        opposite of what a list screen wants and is deliberate here: this manager is
        what the export and the admin lookup go through, and `cnpj__contains=""`
        matching every row would make a cleared search box a full export. A screen that
        wants "no filter" asks `search_filter` for the lookup and gets `None`.
        """
        matches = search_filter(query)
        if matches is None:
            return self.get_queryset().none()
        return self.get_queryset().filter(matches).distinct()
