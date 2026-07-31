"""Every portal template renders under `app_portal`, and none reaches a denied table.

T-081. `visible_nav_items` and the `{% can %}` tag both route to `granted_levels` /
`resolve_level`, which read `authz_capability`, `tenants_membership` and
`authz_rolegrant` — none of which `app_portal` holds SELECT on. Inside a portal request
the C1 stash answers instead, so they work; the hazard is a template that reaches them
*outside* what the stash covers, or a portal template that extends the firm's
`base.html`, which calls `{% nav_items %}` unconditionally.

Today `portal/base.html` is deliberately standalone, so this is latent rather than live.
That is exactly why it is written now: the failure mode is a 500 on a page that used to
work, introduced by an edit that looks like reuse.

Two assertions, because either alone is weak. Rendering proves the templates that exist
today are safe; the structural check proves the *next* one cannot quietly inherit the
firm chrome and take the whole portal down with it.
"""

import re
from http import HTTPStatus
from pathlib import Path
from typing import Final

import pytest
from django.test import Client
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.support import enrol_totp

pytestmark = pytest.mark.django_db(transaction=True)

PORTAL_HOST = "acme-portal.localhost"
PASSWORD = "irrelevant-here"  # noqa: S105

PORTAL_TEMPLATES: Final[Path] = (
    Path(__file__).resolve().parents[2] / "apps" / "portal" / "templates" / "portal"
)

# Anything that resolves a capability at render time. Each reaches granted_levels or
# resolve_level, and both read tables app_portal cannot SELECT.
CAPABILITY_AT_RENDER: Final = re.compile(
    r"\{%\s*nav_items\b|\{%\s*can\b|\|\s*can\b|\bvisible_nav_items\b",
)

# The firm layout. It calls {% nav_items %} unconditionally, so a portal template that
# extends it inherits a query the portal role is denied.
FIRM_LAYOUT: Final = re.compile(r'\{%\s*extends\s+["\'](?!portal/)')


@pytest.fixture(autouse=True)
def _portal_urls(settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]
    monkeypatch.setattr("apps.portal.middleware.PORTAL_URLCONF", "apps.portal.urls")


@pytest.fixture
def portal_user() -> User:
    tenant = Tenant.objects.create(name="Acme", slug="acme")
    with tenant_context(tenant.id):
        # ALL_OBJECTS_OK: fixture seeding, before any portal context exists.
        company = ClientCompany.all_objects.create(
            tenant=tenant,
            legal_name="CLIENTE ACME",
            cnpj="11222333000181",
            is_mei=True,
        )
    user = User.objects.create_user(email="mei@acme.example", password=PASSWORD)
    enrol_totp(user)
    Membership.objects.create(
        user=user,
        tenant=tenant,
        role=TenantRole.CLIENT_OWNER,
        client=company,
    )
    return user


def _templates() -> list[Path]:
    return sorted(PORTAL_TEMPLATES.rglob("*.html"))


def test_the_scan_reaches_the_portal_templates() -> None:
    # Given the template directory
    found = _templates()

    # Then it is not empty. A structural check whose glob matches nothing passes every
    # assertion beneath it, which is the quietest way for this to stop working.
    assert found, f"no templates under {PORTAL_TEMPLATES}"
    assert any(path.name == "base.html" for path in found)


def test_no_portal_template_resolves_a_capability_at_render_time() -> None:
    offenders = [
        f"{path.name}:{number}: {line.strip()}"
        for path in _templates()
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        )
        if CAPABILITY_AT_RENDER.search(line)
    ]

    # The stash covers `can()` inside a portal request, so these would work today -- but
    # only inside it, and only for the capabilities the stash holds. Keeping them out of
    # portal templates entirely means the question never arises during rendering, which
    # happens inside the transaction and under the portal role.
    assert not offenders, offenders


def test_no_portal_template_extends_the_firm_layout() -> None:
    offenders = [
        f"{path.name}: {line.strip()}"
        for path in _templates()
        for line in path.read_text(encoding="utf-8").splitlines()
        if FIRM_LAYOUT.search(line)
    ]

    # portal/base.html is standalone deliberately. Extending the firm layout inherits
    # {% nav_items %}, which queries authz_capability -- denied to app_portal -- and
    # turns every portal page into a 500. The edit that would do it looks like reuse.
    assert not offenders, offenders


def test_the_portal_pages_render_under_the_portal_role(portal_user: User) -> None:
    # Given a signed-in portal user
    http = Client()
    http.force_login(portal_user)

    # When the landing page is rendered -- inside the transaction, as app_portal, with
    # the template rendered before the role reverts
    response = http.get("/", headers={"host": PORTAL_HOST})

    # Then it renders. A denied read here surfaces as ProgrammingError, not as a 500 the
    # test client swallows, so this fails loudly rather than silently.
    assert response.status_code == HTTPStatus.OK
    assert b"CLIENTE ACME" in response.content
