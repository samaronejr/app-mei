"""The allauth entrance screens must be styled *and* survive the portal host.

Four legs, because the two properties this file names — "looks like our product" and
"does not 500 on the client portal" — are each invisible from the other's angle.

The hazard is concrete, not hypothetical. `templates/base.html` renders
`{% url 'dsr-submit' %}` in its footer, and `apps/portal/urls.py` mounts that name
nowhere: the portal tree is a deliberate *subset* of `ROOT_URLCONF`, holding only
`healthz`, `allauth.urls` and three portal views. So any entrance template that reaches
the firm shell raises `NoReverseMatch` the moment a MEI owner opens the login page on
`<slug>-portal.<domain>` — a 500 on the first screen the portal has. The line number
moves every time something is added above it, so the guard below cites the *name*.

`tests/portal/test_portal_templates_render.py` already refuses the firm layout, but its
glob is rooted at `apps/portal/templates/portal/` and cannot see one byte of the tree
this file scans. `templates/allauth/`, `templates/account/` and `templates/mfa/` are a
new boundary, served on BOTH hosts by the same loader, and nothing guarded them.

The second trap is quieter. allauth 65.18.0 ships
`allauth/templates/allauth/layouts/entrance.html` as exactly two lines —
`{% extends "allauth/layouts/base.html" %}` then `{% block content %}{% endblock %}`.
Once an override for the parent exists, Django's loader resolves *that* `extends` to
ours. A layout naming its slot anything other than `content` therefore renders every
entrance page we did NOT override as a blank document: no error, no 500, no log line.
Leg (i)'s extends walk and leg (iii)'s live render are what make that audible.

Legs (i) and (ii) scan a tree the override wave has not created yet. The emptiness check
lives INSIDE `_entrance_templates`, not beside it, because a structural assertion whose
glob matches nothing passes every claim underneath it — the quietest possible way for a
guard to stop guarding.
"""

import re
from http import HTTPStatus
from pathlib import Path
from typing import Final

import pytest
from django.conf import settings
from django.test import Client
from django.urls import get_resolver

from apps.tenants.models import Tenant

# The configured project template directory, read from settings rather than rebuilt from
# `__file__`, so moving either tree cannot leave this scanning a path nobody serves.
PROJECT_TEMPLATES: Final[Path] = Path(settings.TEMPLATES[0]["DIRS"][0])

# The three trees whose contents allauth's own loader will reach. `account` is allauth's
# singular directory; `templates/accounts/` (plural) is this project's invitation and
# team screens and is deliberately NOT in scope — those are firm-host-only and may
# extend the firm shell.
ENTRANCE_ROOTS: Final = ("allauth", "account", "mfa")

# The firm shell, by the name a template would write in `{% extends %}`.
FIRM_SHELL: Final = "base.html"

# The two urlconfs a template in this tree is rendered under. `config.urls` on the firm
# subdomain; `apps.portal.urls` whenever `HostDispatchMiddleware` sees `-portal.` in the
# host, which it does for the identical `/accounts/` paths.
FIRM_URLCONF: Final = "config.urls"
PORTAL_URLCONF: Final = "apps.portal.urls"

# `{% extends "x" %}` / `{% url "x" ... %}` with the template name captured. Both are
# paired with a permissive twin below so a *variable* argument — which no static check
# can follow — is reported rather than silently skipped.
EXTENDS_LITERAL: Final = re.compile(r"""\{%\s*extends\s+(["'])([^"']+)\1""")
EXTENDS_ANY: Final = re.compile(r"\{%\s*extends\s+(\S+)")
URL_LITERAL: Final = re.compile(r"""\{%\s*url\s+(["'])([^"']+)\1""")
URL_ANY: Final = re.compile(r"\{%\s*url\s+(\S+)")

# The firm's primary navigation, as `templates/base.html` emits it: a <nav> carrying the
# `nav-bar` class, wrapped in the disclosure. Its accessible name is the msgid itself —
# `locale/` ships no compiled catalogue — so the label is matched literally too.
FIRM_NAV: Final = re.compile(rb"""<nav\b[^>]*class="[^"]*\bnav-bar\b""", re.IGNORECASE)
FIRM_NAV_LABEL: Final = "Navegação principal".encode()

# The compiled stylesheet, linked by `templates/base.html` and by any layout of ours
# that means to look like this product. Stock allauth links no stylesheet at all, so
# this byte string is present only when one of OUR layouts rendered the page.
STYLE_MARKER: Final = b"css/app.css"

FIRM_SLUG: Final = "acme"
FIRM_HOST: Final = f"{FIRM_SLUG}.localhost"
PORTAL_HOST: Final = f"{FIRM_SLUG}-portal.localhost"
LOGIN_PATH: Final = "/accounts/login/"

# A cycle in `{% extends %}` is a Django error long before it is ours; the bound just
# keeps this walk from hanging while reporting it.
MAX_INHERITANCE_DEPTH: Final = 20


def _entrance_templates() -> list[Path]:
    """Return every template under the allauth-served trees, refusing an empty scan.

    The emptiness check is HERE and not in a sibling test on purpose. Callers below
    iterate this list and assert nothing was found wanting; handed `[]` they would each
    pass while inspecting no bytes at all, and would keep passing after someone deleted
    the tree. Failing inside the helper makes "the scan reached something" a
    precondition of every claim built on it rather than a separate promise.
    """
    found = [
        path
        for root in ENTRANCE_ROOTS
        for path in sorted((PROJECT_TEMPLATES / root).rglob("*.html"))
    ]
    roots = ", ".join(str(PROJECT_TEMPLATES / root) for root in ENTRANCE_ROOTS)
    assert found, (
        f"no templates found under {roots}. Either the entrance overrides were never "
        f"written, or they moved and this scan now inspects nothing — in which case "
        f"every assertion below is vacuously true and guards nothing."
    )
    return found


def _template_name(path: Path) -> str:
    """Return the name Django's loader would resolve this file by."""
    return path.relative_to(PROJECT_TEMPLATES).as_posix()


def _parents() -> dict[str, str]:
    """Map each scanned template to the template it extends, if any.

    A parse rather than a line match: the result is walked transitively below, so a
    layout that reaches the firm shell through one of our own files is caught with the
    same force as one that names it directly.
    """
    parents: dict[str, str] = {}
    unparsed: list[str] = []
    for path in _entrance_templates():
        text = path.read_text(encoding="utf-8")
        literal = EXTENDS_LITERAL.search(text)
        if literal is not None:
            parents[_template_name(path)] = literal.group(2)
            continue
        loose = EXTENDS_ANY.search(text)
        if loose is not None:
            unparsed.append(f"{_template_name(path)}: {{% extends {loose.group(1)} …")

    # `{% extends some_variable %}` defeats every static check in this file, so it is
    # refused outright rather than passed over as "no parent found".
    assert not unparsed, (
        f"a template extends a non-literal parent, which no scan can follow: {unparsed}"
    )
    return parents


def _mounted_names(urlconf: str) -> set[str]:
    """Return every URL name reversible under `urlconf`, namespaces included.

    Reads the resolver's own reverse map instead of calling `reverse()`, because a name
    that IS mounted but needs arguments raises `NoReverseMatch` too, and this leg asks
    whether the name exists — not whether it can be built without arguments.
    """
    resolver = get_resolver(urlconf)
    names = {key for key in resolver.reverse_dict if isinstance(key, str)}
    for namespace, (_, sub_resolver) in resolver.namespace_dict.items():
        names.update(
            f"{namespace}:{key}"
            for key in sub_resolver.reverse_dict
            if isinstance(key, str)
        )
    return names


@pytest.fixture
def firm() -> Tenant:
    """The one tenant both hosts below resolve to."""
    return Tenant.objects.create(name="Acme Contabilidade", slug=FIRM_SLUG)


@pytest.fixture(autouse=True)
def _hosts() -> None:
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]


# ------------------------------------------------------------------ (i) the boundary


def test_no_entrance_template_reaches_the_firm_shell() -> None:
    # Given the inheritance graph of every template allauth's loader will reach
    parents = _parents()

    # When each chain is walked to its root
    offenders: list[str] = []
    for name in sorted(parents):
        chain = [name]
        current = name
        for _ in range(MAX_INHERITANCE_DEPTH):
            parent = parents.get(current)
            if parent is None:
                break
            chain.append(parent)
            if parent == FIRM_SHELL:
                offenders.append(" -> ".join(chain))
                break
            current = parent

    # Then none of them ends at templates/base.html. That shell renders
    # {% url 'dsr-submit' %}, a name apps/portal/urls.py does not mount, so reaching it
    # turns the portal login page into a NoReverseMatch 500 -- on the first screen a MEI
    # owner ever sees, and only on the host nobody develops against.
    assert not offenders, offenders


# ------------------------------------------------------------- (ii) reversibility


def test_every_entrance_url_reverses_on_both_hosts() -> None:
    # Given the URL names written into the entrance tree
    firm_names = _mounted_names(FIRM_URLCONF)
    portal_names = _mounted_names(PORTAL_URLCONF)

    unparsed: list[str] = []
    offenders: list[str] = []
    for path in _entrance_templates():
        text = path.read_text(encoding="utf-8")
        literals = {match.group(2) for match in URL_LITERAL.finditer(text)}
        # Every {% url %} in the file, so a variable argument is reported rather than
        # quietly excluded from the literal set above.
        unparsed.extend(
            f"{_template_name(path)}: {{% url {match.group(1)} …"
            for match in URL_ANY.finditer(text)
            if not match.group(1).startswith(('"', "'"))
        )
        for name in sorted(literals):
            missing = [
                urlconf
                for urlconf, mounted in (
                    (FIRM_URLCONF, firm_names),
                    (PORTAL_URLCONF, portal_names),
                )
                if name not in mounted
            ]
            if missing:
                offenders.append(
                    f"{_template_name(path)}: {{% url '{name}' %}} "
                    f"is not mounted by {', '.join(missing)}",
                )

    assert not unparsed, (
        f"a template reverses a non-literal URL name, which no scan can check: "
        f"{unparsed}"
    )

    # Then every one of them resolves under BOTH trees. `account_*` and `mfa_*` do,
    # because apps/portal/urls.py includes allauth.urls at the identical prefix.
    # `dsr-submit` does not, and it is exactly the name templates/base.html contributes.
    assert not offenders, offenders


# --------------------------------------------------------- (iii) the portal host, live


@pytest.mark.django_db(transaction=True)
def test_the_login_page_renders_styled_on_the_portal_host(firm: Tenant) -> None:
    # Given a portal hostname for a real firm
    assert firm.slug == FIRM_SLUG

    # When an anonymous visitor opens the login page there
    response = Client().get(LOGIN_PATH, headers={"host": PORTAL_HOST})

    # Then it is a rendered page and not a NoReverseMatch, a redirect to somewhere
    # else, or the enrolment loop. Status alone would be satisfied by allauth's stock
    # unstyled document, so the compiled stylesheet is required in the same breath:
    # together they say "this page exists AND it is ours".
    assert response.status_code == HTTPStatus.OK, (
        f"portal login answered {response.status_code}; "
        f"{response.get('Location', 'no Location header')}"
    )
    assert STYLE_MARKER in response.content, (
        f"portal login rendered without {STYLE_MARKER!r}: no layout of ours ran"
    )

    # And it carries none of the firm chrome. The firm nav resolves capabilities through
    # granted_levels, which reads three tables app_portal holds no SELECT on.
    assert not FIRM_NAV.search(response.content), "firm nav rendered on the portal host"
    assert FIRM_NAV_LABEL not in response.content


# ------------------------------------------------------------ (iv) the firm host, live


@pytest.mark.django_db(transaction=True)
def test_the_login_page_stays_styled_on_the_firm_host(firm: Tenant) -> None:
    # Given the firm's own subdomain
    assert firm.slug == FIRM_SLUG

    # When an anonymous visitor opens the same path there
    response = Client().get(LOGIN_PATH, headers={"host": FIRM_HOST})

    # Then it renders and is styled. The portal-safe override is the change that could
    # break this: a layout written to avoid the firm shell must not take the firm's own
    # entrance screens down to unstyled allauth defaults on the way.
    assert response.status_code == HTTPStatus.OK, (
        f"firm login answered {response.status_code}; "
        f"{response.get('Location', 'no Location header')}"
    )
    assert STYLE_MARKER in response.content, (
        f"firm login rendered without {STYLE_MARKER!r}: no layout of ours ran"
    )
