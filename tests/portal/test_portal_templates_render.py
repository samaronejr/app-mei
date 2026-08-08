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

# ------------------------------------------------------- the relation-traversal ban
#
# Everything above asks whether a template resolves a *capability* while rendering. The
# cases at the foot of this module ask the wider question: whether it follows a
# *relation* while rendering. Both run in the same place — inside the transaction, under
# the portal role — and both fail the same way, as a 500 on a page that used to work.
#
# The difference is that a relation is easy to write by accident. `{{ document.x }}` and
# `{{ obligation.y }}` read like attribute access and compile to a second query against
# a table `app_portal` holds no SELECT on. A view that hands the template a scalar it
# already fetched cannot make that mistake; a template reaching through the object can.

# Every template the shell and its four pages are made of, relative to the portal
# template root. Enumerated rather than counted so that a file renamed or deleted reds
# here, instead of silently shrinking the set every scan in this module quantifies over.
EXPECTED_TEMPLATES: Final = frozenset(
    {
        "base.html",
        "home.html",
        "payments.html",
        "documents.html",
        "account.html",
        "partials/nav.html",
    },
)

# Relations a portal template must never follow, and the tables they reach:
#
#   uploaded_by       obligations_document -> accounts_user
#   obligation_type.  obligations_obligation -> obligations_obligationtype, which is
#                     NOT in the allow-list. Its primary key IS the code, so a view can
#                     hand down a scalar with no join at all; a template following the
#                     relation issues one. A view-provided scalar such as `type_label`
#                     is fine, which is why the dot is part of the literal.
#   nav_items         authz_capability, tenants_membership, authz_rolegrant
#   {% can            the same three, through resolve_level
FORBIDDEN_TRAVERSALS: Final = ("uploaded_by", "obligation_type.", "nav_items", "{% can")

# A dotted path through `.user.`, e.g. `request.user.email` in an interpolation. The
# whole path is captured so it can be compared against the allow-list below rather than
# merely detected.
USER_TRAVERSAL: Final = re.compile(r"\w+(?:\.\w+)*\.user(?:\.\w+)+")

# The three attributes the middleware has already materialised by the time a portal
# template renders. `request.user` is resolved before the transaction opens and before
# the role switch, so these three cost no query; anything further through the same
# object — a membership, a permission set, an email address row — costs one, against a
# table the portal role cannot read. `apps/portal/templates/portal/base.html` documents
# the same fact at the point of use.
ALLOWED_USER_PATHS: Final = frozenset(
    {
        "request.user.email",
        "request.user.pk",
        "request.user.is_authenticated",
    },
)


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


def _relative_names() -> set[str]:
    return {path.relative_to(PORTAL_TEMPLATES).as_posix() for path in _templates()}


def test_the_scan_reaches_the_shell_and_all_four_pages() -> None:
    # Given the shipped portal template tree
    found = _relative_names()

    # Then every file the shell is assembled from is in it. The scans below quantify
    # over this set, so a page that never joined it is a page nothing checks.
    assert found >= EXPECTED_TEMPLATES, sorted(EXPECTED_TEMPLATES - found)


def test_no_portal_template_follows_a_relation_while_rendering() -> None:
    # Given every portal template, read line by line
    offenders = [
        f"{path.relative_to(PORTAL_TEMPLATES).as_posix()}:{number}: {line.strip()}"
        for path in _templates()
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        )
        for literal in FORBIDDEN_TRAVERSALS
        if literal in line
    ]

    # Then none of them reaches through a foreign key. Each of these compiles to a JOIN
    # or a second query issued during rendering -- inside the transaction, under the
    # portal role -- against a table app_portal holds no SELECT on, so the page 500s.
    # The fix is always the same shape: have the view pass down the scalar it already
    # has, and let the template render a value rather than fetch one.
    assert not offenders, offenders


def test_no_portal_template_reaches_past_the_materialised_user() -> None:
    # Given every dotted path through `.user.` any portal template writes
    written = [
        (f"{path.relative_to(PORTAL_TEMPLATES).as_posix()}:{number}", match.group(0))
        for path in _templates()
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        )
        for match in USER_TRAVERSAL.finditer(line)
    ]

    # Then the shell writes at least one, or the allow-list below is a statement about
    # nothing and a template that lost the account line entirely would pass.
    assert written, (
        "no portal template reads request.user at all, so the allow-listed attributes "
        "below are unexercised and this case cannot fail"
    )

    # And every one of them is an attribute the middleware materialised before the
    # transaction opened. Anything further -- a membership, a permission, an email
    # address row -- is a query under a role that cannot read the table it lands on.
    offenders = [
        f"{where}: {path}" for where, path in written if path not in ALLOWED_USER_PATHS
    ]
    assert not offenders, offenders
