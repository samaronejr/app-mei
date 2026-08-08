"""Every partial shared by both products renders from context alone.

`templates/partials/pure/` is the only presentation code the accountant firm
workspace and the RLS-isolated MEI client portal both include. The portal renders
inside a transaction under `app_portal`, a role holding SELECT on seven tables and
nothing else, so a shared partial that resolves a capability or reaches the ORM at
render time is a 500 on a real client's primary path -- raised from a file the firm
side exercises constantly and never fails on.

Two legs, because either alone is weak. The source scan proves the capability
vocabulary is absent, which holds even for a branch no fixture happens to render.
The render proves the partials that exist today cost nothing, which catches a query
arriving through a variable no source scan can see.

Both legs quantify over a glob, and `templates/partials/pure/` does not exist until
the partials are written -- `rglob` over a missing directory yields nothing rather
than raising. So `_pure_partials` refuses to hand back a set that proves nothing,
and it is the only way in: a glob that matches no file must fail there, never pass
vacuously in every assertion beneath it.
"""

import re
from pathlib import Path
from typing import Any, Final

import pytest
from django.conf import settings
from django.db import connection
from django.template import engines
from django.test.utils import CaptureQueriesContext

# The loader root, not BASE_DIR: `relative_to` against it yields the template *name*
# the engine resolves, so the render leg can never drift from the scanned file.
TEMPLATE_ROOT: Final[Path] = Path(settings.TEMPLATES[0]["DIRS"][0])
PURE_PARTIALS: Final[Path] = TEMPLATE_ROOT / "partials" / "pure"

# Named individually rather than counted. A count is satisfied by any three files,
# and these three are the ones the portal templates are going to include.
REQUIRED: Final[tuple[str, ...]] = ("field.html", "form_errors.html", "notice.html")

# `{% load navigation %}` / `{% load authz %}`. Either pulls in a tag that routes to
# granted_levels or resolve_level, and both read `authz_capability`,
# `tenants_membership` and `authz_rolegrant` -- none of which app_portal holds.
# Loading is caught rather than only calling, because the load is the moment the
# partial stops being shareable; the call site can arrive in a later edit.
CAPABILITY_LIBRARY: Final = re.compile(r"\{%\s*load\b[^%]*\b(?:navigation|authz)\b")

# The render-time capability vocabulary itself, matched as bare words so that a call
# written through a filter, an `{% if %}` guard or an attribute is caught the same
# way as a tag. Deliberately stricter than the portal's CAPABILITY_AT_RENDER: that
# one polices templates the portal owns, this one polices templates two products
# share, where the cost of a false negative is paid by whichever product is less
# tested. A false positive here is a rewording, which is the cheaper mistake.
FORBIDDEN_TOKENS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("nav_items", re.compile(r"\bnav_items\b")),
    ("visible_nav_items", re.compile(r"\bvisible_nav_items\b")),
    ("can", re.compile(r"\bcan\b")),
    ("perms.", re.compile(r"\bperms\.")),
)

# Plain data only. A model instance, a queryset or a bound form here would make the
# query count a property of the fixture rather than of the partial, which is exactly
# the confusion this leg exists to remove. Unused keys cost nothing: Django resolves
# a missing variable to the empty string, so one context serves every partial.
CONTEXT: Final[dict[str, Any]] = {
    "label": "Razão social",
    "name": "legal_name",
    "field_id": "id_legal_name",
    "value": "ACME LTDA",
    "help_text": "Como consta no CNPJ.",
    "required": True,
    "errors": ["Campo obrigatório."],
    "message": "Alterações salvas.",
    "variant": "success",
}


def _pure_partials() -> list[Path]:
    """Return every pure partial, refusing to return a set that proves nothing.

    The non-vacuity assertions live here rather than in one test, so that no purity
    check below can quantify over an empty set even when run in isolation. This is
    the only entry point to the scan for that reason.
    """
    found = sorted(PURE_PARTIALS.rglob("*.html"))
    assert found, (
        f"no templates under {PURE_PARTIALS}: a glob that matches nothing satisfies "
        "every purity assertion below it, which is the quietest way for this guard "
        "to stop working"
    )

    names = {path.name for path in found}
    missing = [required for required in REQUIRED if required not in names]
    assert not missing, (
        f"missing from {PURE_PARTIALS}: {missing}; found {sorted(names)}"
    )
    return found


def _offenders(pattern: re.Pattern[str]) -> list[str]:
    """Return `file:line: source` for every line a forbidden pattern matches."""
    return [
        f"{path.relative_to(TEMPLATE_ROOT).as_posix()}:{number}: {line.strip()}"
        for path in _pure_partials()
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        )
        if pattern.search(line)
    ]


def test_the_scan_reaches_the_pure_partials() -> None:
    # Given the shared partial directory
    found = _pure_partials()

    # Then it holds the three partials both products include, by name. Asserted
    # before either purity leg runs, because both are structural checks over the
    # same glob and a glob matching nothing passes them without reading a file.
    assert {path.name for path in found} >= set(REQUIRED)


def test_no_pure_partial_loads_a_capability_tag_library() -> None:
    offenders = _offenders(CAPABILITY_LIBRARY)

    # A pure partial that loads `navigation` or `authz` is one edit away from
    # calling into it, and that call reads tables app_portal is denied.
    assert not offenders, f"a pure partial loads a capability tag library: {offenders}"


def test_no_pure_partial_names_the_render_time_capability_vocabulary() -> None:
    offenders = [
        f"{token}: {location}"
        for token, pattern in FORBIDDEN_TOKENS
        for location in _offenders(pattern)
    ]

    # Every one of these resolves a capability while the template renders -- inside
    # the portal's transaction, under the portal's role. A shared partial answers to
    # both products, so it may not ask a question only one of them can answer.
    assert not offenders, (
        f"a pure partial names the render-time capability vocabulary: {offenders}"
    )


@pytest.mark.django_db
def test_every_pure_partial_renders_from_context_alone_without_a_query() -> None:
    """Rendering a pure partial costs zero queries, from plain data.

    Counted per template and collected rather than asserted through
    `django_assert_num_queries`, whose failure names a number and not the file that
    produced it. A guard nobody can act on is a guard that gets deleted.
    """
    engine = engines["django"]
    offenders: list[str] = []

    for path in _pure_partials():
        name = path.relative_to(TEMPLATE_ROOT).as_posix()
        template = engine.get_template(name)
        with CaptureQueriesContext(connection) as captured:
            template.render(CONTEXT)
        if captured.captured_queries:
            sql = [query["sql"] for query in captured.captured_queries]
            offenders.append(f"{name}: {sql}")

    assert not offenders, (
        f"a pure partial queried the database while rendering: {offenders}"
    )
