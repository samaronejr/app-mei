"""A valid certificate does not mean Django will answer.

`ALLOWED_HOSTS` is checked before anything else in the request cycle, so a portal host
that is missing from it gets a bare 400 `DisallowedHost` from behind a perfectly good
wildcard certificate. "It resolves and serves TLS" does not cover that, which is why
V2's exit criteria name it separately.

The rule is asserted against the host SHAPE rather than against one domain. The domain
in use is explicitly temporary, and a test pinned to it would have to be edited on the
day the real name is chosen — exactly when nobody wants to be reasoning about whether
the edit was safe.
"""

import pytest
from django.http.request import validate_host

from apps.tenants.validators import PORTAL_HOST_SUFFIX

FIRM_HOST = "acme.example.dev"
PORTAL_HOST = f"acme{PORTAL_HOST_SUFFIX}.example.dev"
SUBDOMAIN_WILDCARD = [".example.dev"]


def test_the_portal_host_is_one_label_so_one_wildcard_covers_both() -> None:
    # Given the firm host and the portal host derived from the same slug
    firm_label = FIRM_HOST.split(".", 1)[0]
    portal_label = PORTAL_HOST.split(".", 1)[0]

    # Then both are a SINGLE DNS label. This is the entire reason the portal host is
    # <slug>-portal.<domain> and not portal.<slug>.<domain>: a public wildcard covers
    # exactly one label, so the two-label form would need a certificate no CA issues.
    assert "." not in firm_label
    assert "." not in portal_label
    assert portal_label.endswith(PORTAL_HOST_SUFFIX)


def test_a_leading_dot_entry_admits_both_the_firm_and_the_portal_host() -> None:
    # Given the ALLOWED_HOSTS shape base.py documents for subdomain tenancy
    # Then Django admits both, so a portal request is not refused before it is routed
    assert validate_host(FIRM_HOST, SUBDOMAIN_WILDCARD)
    assert validate_host(PORTAL_HOST, SUBDOMAIN_WILDCARD)


def test_the_entry_still_refuses_a_foreign_host() -> None:
    # Then the wildcard has not been widened into "anything goes", which would make the
    # assertions above pass for the wrong reason
    assert not validate_host("evil.test", SUBDOMAIN_WILDCARD)
    assert not validate_host("example.dev.evil.test", SUBDOMAIN_WILDCARD)


@pytest.mark.parametrize(
    "allowed",
    [
        ["example.dev"],
        ["acme.example.dev"],
    ],
)
def test_an_entry_without_the_leading_dot_refuses_the_portal_host(
    allowed: list[str],
) -> None:
    # Given ALLOWED_HOSTS naming the apex, or one firm host, but no subdomain wildcard
    # Then the portal host is refused. This is the misconfiguration the criterion
    # exists for: DNS resolves, the certificate is valid, and every portal request
    # still answers 400 with nothing in the certificate chain to explain it.
    assert not validate_host(PORTAL_HOST, allowed)
