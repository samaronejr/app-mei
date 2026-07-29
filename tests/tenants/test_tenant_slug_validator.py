"""A firm's slug decides another firm's portal host, so the namespace is reserved.

The portal is served at `<slug>-portal.<domain>`. That makes the slug namespace
load-bearing in two directions at once, and `Tenant.slug` is a plain unique SlugField:

* A firm registering `acme-portal` is served at `acme-portal.<domain>`, which IS the
  portal host of `acme` — it would receive `acme`'s portal traffic.
* Host dispatch routes `*-portal` to the portal urlconf, so that same firm's own front
  door would resolve to the portal rather than to the application.

Neither failure raises. Both produce a working host serving the wrong thing.

The length cap is the same constraint one layer down: a DNS label stops at 63 characters
and `-portal` costs 7, so a 63-character slug derives a 70-character label that resolves
nowhere.

The validator and the startup check are NOT redundant. The validator guards rows written
through a form or an explicit `full_clean()`; the check is the only thing that sees rows
already in the table, including any written by a path that skipped validation.
"""

import pytest
from django.core.exceptions import ValidationError

from apps.core.checks import check_tenant_slugs
from apps.tenants.models import Tenant
from apps.tenants.validators import (
    MAX_TENANT_SLUG_LENGTH,
    PORTAL_HOST_SUFFIX,
    validate_tenant_slug,
)

pytestmark = pytest.mark.django_db

DNS_LABEL_LIMIT = 63


def test_the_cap_is_exactly_what_the_dns_label_limit_allows() -> None:
    # Given the derived host <slug>-portal
    # Then the cap plus the suffix is exactly the DNS label limit. Written as an
    # assertion because a future edit to either constant must move the other.
    assert MAX_TENANT_SLUG_LENGTH + len(PORTAL_HOST_SUFFIX) == DNS_LABEL_LIMIT


def test_a_slug_ending_in_portal_is_rejected() -> None:
    # Given a firm trying to register acme-portal
    # When the slug is validated
    with pytest.raises(ValidationError) as caught:
        validate_tenant_slug("acme-portal")

    # Then it is refused, naming the firm whose portal host it would take over
    assert caught.value.code == "tenant_slug_reserved"
    assert "acme" in str(caught.value)


def test_an_ordinary_slug_containing_portal_is_allowed() -> None:
    # Given slugs that merely contain the word rather than ending in the suffix
    # Then none is refused. Rejecting these would be a guard that cries wolf: only the
    # suffix produces the collision, because only the suffix is what dispatch strips.
    for slug in ("portal", "portalis", "portal-acme", "acme-portal-ltda"):
        validate_tenant_slug(slug)


def test_the_length_cap_is_enforced_at_the_boundary() -> None:
    # Given a slug of exactly the cap, and one character more
    at_limit = "a" * MAX_TENANT_SLUG_LENGTH
    over_limit = "a" * (MAX_TENANT_SLUG_LENGTH + 1)

    # Then the first is accepted and the second refused, so the boundary is pinned
    # rather than merely somewhere in the right region.
    validate_tenant_slug(at_limit)
    with pytest.raises(ValidationError) as caught:
        validate_tenant_slug(over_limit)
    assert caught.value.code == "tenant_slug_too_long"


def test_the_model_field_actually_runs_the_validator() -> None:
    # Given a Tenant built with a reserved slug
    tenant = Tenant(name="Firma", slug="acme-portal")

    # When it is cleaned as a form or a full_clean() would
    with pytest.raises(ValidationError) as caught:
        tenant.full_clean()

    # Then the field carries the validator. Without this the function above could be
    # correct and wired to nothing.
    assert "slug" in caught.value.message_dict


def test_the_startup_check_is_silent_on_a_clean_table() -> None:
    # Given only ordinary slugs
    Tenant.objects.create(name="Firma", slug="firma-limpa")

    # Then nothing is reported, so the check below cannot be passing for a trivial
    # reason
    assert check_tenant_slugs(None) == []


def test_the_startup_check_sees_a_slug_the_validator_never_guarded() -> None:
    # Given a row written by a path that skips validation, as bulk paths and any row
    # predating the validator do
    Tenant.objects.bulk_create([Tenant(name="Antiga", slug="acme-portal")])

    # When the check runs
    messages = check_tenant_slugs(None)

    # Then it is reported. This is the only control that sees such a row: the validator
    # is never invoked on it.
    assert [message.id for message in messages] == ["core.E009"]
    assert "acme-portal" in messages[0].msg
