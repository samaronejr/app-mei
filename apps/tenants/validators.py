"""What a firm's slug may be, given that the portal host is derived from it.

The portal is served at `<slug>-portal.<domain>`, chosen over `portal.<slug>.<domain>`
because four labels cannot get a public certificate: a wildcard covers exactly one
label, and no public CA issues `*.*.example.com`.

Deriving one host from another makes the slug namespace load-bearing in two directions,
and `Tenant.slug` is a plain unique SlugField with no reserved names:

* A firm registering `acme-portal` would be served at `acme-portal.<domain>`, which IS
  `acme`'s portal host. It would receive `acme`'s portal traffic.
* The same collision runs the other way. Host dispatch routes `*-portal` to the portal
  urlconf, so that firm's own front door would resolve to the portal instead of the
  application.

Length matters for the same reason. A DNS label is at most 63 characters and `-portal`
costs 7, so a 63-character slug produces a 70-character label that cannot be resolved at
all. The column still allows 63 for the firm-side host; this caps what may be stored so
the derived host stays legal.
"""

from typing import Final

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

PORTAL_HOST_SUFFIX: Final = "-portal"

# 63-character DNS label limit, less "-portal".
MAX_TENANT_SLUG_LENGTH: Final = 56


def validate_tenant_slug(value: str) -> None:
    """Reject a slug that would collide with a portal host or overflow a DNS label."""
    if value.endswith(PORTAL_HOST_SUFFIX):
        raise ValidationError(
            _(
                "A slug may not end in %(suffix)s: %(value)s would be served at "
                "%(value)s.<domain>, which is the portal host of the firm "
                "%(stolen)s.",
            ),
            code="tenant_slug_reserved",
            params={
                "suffix": PORTAL_HOST_SUFFIX,
                "value": value,
                "stolen": value[: -len(PORTAL_HOST_SUFFIX)] or "(empty)",
            },
        )
    if len(value) > MAX_TENANT_SLUG_LENGTH:
        raise ValidationError(
            _(
                "A slug may be at most %(limit)d characters so that "
                "%(value)s%(suffix)s stays a valid DNS label; this one is "
                "%(length)d.",
            ),
            code="tenant_slug_too_long",
            params={
                "limit": MAX_TENANT_SLUG_LENGTH,
                "value": value,
                "suffix": PORTAL_HOST_SUFFIX,
                "length": len(value),
            },
        )
