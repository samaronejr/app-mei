"""The template side of `can()`, so a screen hides what it would refuse.

Registered as an assignment tag rather than a filter because a permission check takes
three arguments and a filter takes one, and because `{% can ... as name %}` evaluates
once per render instead of once per reference.
"""

from django import template

from apps.authz.services import Actor, can

register = template.Library()


@register.simple_tag(name="can")
def can_tag(user: Actor, action: str, obj: object | None = None) -> bool:
    """Answer the same question `can()` answers, for use in `{% if %}`."""
    return can(user, action, obj)
