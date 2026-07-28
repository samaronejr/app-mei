"""Expose the navigation table to the base template.

An assignment tag rather than a context processor: a context processor would resolve
capabilities on every render, including the anonymous public pages that draw no nav at
all, and each resolution is a database read.
"""

from django import template

from apps.authz.services import Actor
from apps.core.navigation import ResolvedNavItem, visible_nav_items

register = template.Library()


@register.simple_tag(name="nav_items")
def nav_items_tag(user: Actor) -> tuple[ResolvedNavItem, ...]:
    """Return the nav destinations this account may reach."""
    return visible_nav_items(user)
