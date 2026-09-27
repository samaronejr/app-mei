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

from django.conf import settings
from django.http import HttpRequest
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

from apps.authz.models import GrantLevel
from apps.authz.services import Actor, granted_levels
from apps.core.flags import tenant_has
from apps.cycles.capabilities import PLAYBOOK_GATE


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


# The flag that swaps the MEI-era bar for the product shell (plan S5.3).
PRODUCT_SHELL_FLAG: Final = "cycles_r1"

# The product shell's top level, in the S5.3 order: Hoje, Clientes, Ciclos,
# Documentos, Comunicação, Gestão. Only destinations that exist are listed — a
# module joins this tuple in the change that ships its route, because an entry
# pointing at nothing is a dead link on every page, and "every shipped destination
# resolves" (tests/ui/test_navigation.py) holds for this tuple exactly as for NAV.
#
# Hoje points at the dashboard until the briefing replaces it at the same address,
# and Gestão at the team screen until playbooks and automations join it. The
# MEI-era queues are reached from the dashboard's own counts, as they are today.
#
# Documentos joins when its working review route ships (AXR1-013).
NAV_R1: Final[tuple[NavItem, ...]] = (
    NavItem(_("Hoje"), "dashboard", "clients.view_assigned"),
    NavItem(_("Clientes"), "clients-list", "clients.view_assigned"),
    NavItem(_("Ciclos"), "cycle-list", "clients.view_assigned"),
    NavItem(_("Comunicação"), "delivery-queue", "clients.view_assigned"),
    # Playbooks join the shell here, before Gestão: the first working
    # destination under the gestão umbrella, gated on playbooks.manage.
    NavItem(_("Playbooks"), "playbook-list", PLAYBOOK_GATE),
    NavItem(_("Gestão"), "team", "users.create"),
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


@dataclass(frozen=True, slots=True)
class ProductShell:
    """What the product shell draws: the wordmark, the firm, and its destinations."""

    product_name: str
    tenant_name: str
    items: tuple[ResolvedNavItem, ...]


def product_shell(request: HttpRequest | None) -> ProductShell | None:
    """Return the product shell for this request, or None for the MEI-era one.

    None unless a firm context was GRANTED (`request.tenant`, set by
    `TenantMiddleware` only after the membership check), the account is signed
    in, and the firm carries the flag. Every other page — anonymous, platform
    host, a flag-off firm — keeps the header it had, byte for byte.

    `tenant_has` answers from the per-request flag cache the middleware primed,
    so the check itself costs no query; only the flag-on branch resolves the
    capability table, exactly as the MEI-era bar does.
    """
    if request is None:
        return None
    tenant = getattr(request, "tenant", None)
    user = getattr(request, "user", None)
    if tenant is None or user is None or not user.is_authenticated:
        return None
    if not tenant_has(tenant.pk, PRODUCT_SHELL_FLAG):
        return None
    return ProductShell(
        product_name=settings.PRODUCT_NAME,
        tenant_name=tenant.name,
        items=visible_nav_items(user, NAV_R1),
    )


__all__ = [
    "NAV",
    "NAV_R1",
    "PRODUCT_SHELL_FLAG",
    "NavItem",
    "ProductShell",
    "ResolvedNavItem",
    "product_shell",
    "visible_nav_items",
]
