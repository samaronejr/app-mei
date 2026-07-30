"""Which hostnames are portal hostnames.

A leaf module on purpose. Both `apps/portal/middleware.py` and
`apps/tenants/middleware.py` must answer this question — the latter to no-op itself on a
portal host — and putting the function in either middleware would make the two import
each other. This imports only from `apps/tenants/validators.py`, which imports only
Django.
"""

from apps.tenants.validators import PORTAL_HOST_SUFFIX

MIN_LABELS_FOR_A_SUBDOMAIN = 2


def portal_slug_from_host(host: str) -> str | None:
    """Return the firm slug for a portal hostname, or None if this is not one.

    `slug_from_host` is deliberately not reused. It returns the leading label verbatim,
    which for `acme-portal.example.com` is the string `"acme-portal"` — a slug no tenant
    owns, because the reserved-slug validator forbids exactly that shape. The suffix
    must be *required* and *stripped*, and a host without it is not a portal host.
    """
    # Lowercased because DNS is case-insensitive and `get_host()` returns the Host
    # header VERBATIM. Without this, `ACME-PORTAL.example.com` fails the suffix test,
    # every portal middleware no-ops, and the request is served the FIRM's URL tree —
    # `/admin/` included — while the browser still sends the portal session cookie,
    # cookie domains being case-insensitive. The entire isolation layer would be
    # bypassed by changing the case of a header.
    hostname = host.split(":", 1)[0].lower()
    labels = hostname.split(".")
    if len(labels) < MIN_LABELS_FOR_A_SUBDOMAIN:
        return None
    label = labels[0]
    if not label.endswith(PORTAL_HOST_SUFFIX):
        return None
    return label[: -len(PORTAL_HOST_SUFFIX)] or None
