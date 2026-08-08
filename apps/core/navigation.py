"""What the navigation bar offers this account, and nothing more.

A hidden link is not a permission control — every destination is independently guarded
by `require_can`, and removing this module would change no authorization outcome. What
it changes is honesty: an interface that offers a firm owner's screens to a staff
accountant and then refuses them teaches people to distrust the interface.

Items are declared with a capability rather than a role. Roles are read from the same
matrix `can()` reads, so a cell edited in `apps/authz/matrix.py` moves the nav with it
and there is no second list to forget.
"""

from dataclasses import dataclass
from typing import Final

from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

from apps.authz.models import GrantLevel
from apps.authz.services import Actor, granted_levels


@dataclass(frozen=True, slots=True)
class NavItem:
    """One destination, and the capability that earns it."""

    label: object
    url_name: str
    capability: str
    url_args: tuple[object, ...] = ()

    def url(self) -> str:
        """Reverse the destination. Every shipped item must resolve."""
        return reverse(self.url_name, args=self.url_args)


@dataclass(frozen=True, slots=True)
class ResolvedNavItem:
    """A nav item with its URL already reversed, ready for the template."""

    label: object
    url: str


NAV: Final[tuple[NavItem, ...]] = (
    NavItem(_("Painel"), "dashboard", "clients.view_assigned"),
    NavItem(_("Clientes"), "clients-list", "clients.view_assigned"),
    NavItem(_("Vencendo"), "queue-due-soon", "das.generate"),
    NavItem(_("Atrasadas"), "queue-overdue", "das.generate"),
    NavItem(_("Onboarding"), "queue-onboarding", "clients.view_assigned"),
    NavItem(_("Limite"), "queue-threshold", "obligations.view_revenue_threshold_queue"),
    NavItem(
        pgettext_lazy("navigation", "Exportar clientes"),
        "clients-export-csv",
        "clients.view_all",
    ),
    NavItem(_("Equipe"), "team", "users.create"),
)


def visible_nav_items(
    user: Actor,
    items: tuple[NavItem, ...] = NAV,
) -> tuple[ResolvedNavItem, ...]:
    """Return the destinations this account may actually reach.

    Only `full` earns a nav entry. The refined levels — `limited`, `own`, and the two
    channel levels — describe what an account may do *to a particular object*, and a
    bar has no object in hand, so drawing them would offer a link that the object-level
    check refuses on arrival. This is exactly the answer `can()` gives with no object;
    the bulk resolution is a query optimisation, not a second opinion.
    """
    if not user.is_authenticated:
        return ()
    levels = granted_levels(user, [item.capability for item in items])
    return tuple(
        ResolvedNavItem(label=item.label, url=item.url())
        for item in items
        if levels[item.capability] == GrantLevel.FULL
    )


__all__ = ["NAV", "NavItem", "ResolvedNavItem", "visible_nav_items"]
