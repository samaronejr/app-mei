"""The client identity may be written in two modules and nowhere else.

`app.client_id` is the whole portal boundary: the RESTRICTIVE policies compare against
it, so whoever can write it can choose which client's rows a session sees. It must come
from the membership row and from nothing a caller can influence.

**The first version of this guard could not fire.** It searched for the literal
`set_config('app.client_id'`, which appears nowhere in `apps/` — every GUC write here
uses bound parameters, so the string is unreachable by house style. An allow-list
matching zero occurrences is satisfied identically by a clean tree and a compromised
one. It was wrong in the other
direction too: that literal *does* occur in shipped Wave-1 test code, so widening the
scan to the repo would fail on legitimate files.

Worst of all it missed the real vector. `client_context()` is exported, request-callable
and writes both the GUC and the ContextVar — from *inside* a permitted module. A view
calling `client_context(request.GET["client_id"])` inside a portal transaction opens a
genuine cross-client read window, and the old pattern saw nothing.

So the guard matches the **constant**, the **raw name**, the **ContextVar** and the
**wrapper**, and this module proves it matches the real call site rather than trusting
that it does.
"""

import re
from pathlib import Path
from typing import Final

import pytest

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
APPS_ROOT: Final[Path] = PROJECT_ROOT / "apps"

CLIENT_ID_WRITE: Final = re.compile(
    r"\bCLIENT_GUC\b"
    r"|[\"']app\.client_id[\"']"
    r"|\bcurrent_client_id\.set\("
    r"|\bclient_context\(",
)

# `apps/core/tenancy.py` owns the primitives; `apps/portal/middleware.py` is the one
# caller that may write the identity, and only from `membership.client_id`.
PERMITTED: Final[frozenset[str]] = frozenset(
    {
        "apps/core/tenancy.py",
        "apps/portal/middleware.py",
    },
)

LEGITIMATE_WRITE_SITE: Final = "apps/portal/middleware.py"

FORGERY = (
    'cursor.execute("SELECT set_config(%s, %s, true)", '
    '[CLIENT_GUC, request.GET["client_id"]])'
)


def _without_comments(source: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in source.splitlines())


def _offenders() -> list[str]:
    found = []
    for path in APPS_ROOT.rglob("*.py"):
        # Migrations are excluded, as the role-shape guard already excludes them. The
        # policy predicates in apps/core/migrations/_operations.py must name
        # app.client_id in order to DEFINE the policies that read it, and a migration
        # has no request to forge an identity from in the first place.
        if "__pycache__" in path.parts or "migrations" in path.parts:
            continue
        relative = str(path.relative_to(PROJECT_ROOT))
        if relative in PERMITTED:
            continue
        code = _without_comments(path.read_text(encoding="utf-8"))
        for number, line in enumerate(code.splitlines(), start=1):
            if CLIENT_ID_WRITE.search(line):
                found.append(f"{relative}:{number}: {line.strip()}")
    return found


def test_the_scan_reaches_the_codebase() -> None:
    # Given the apps tree
    sources = [p for p in APPS_ROOT.rglob("*.py") if "__pycache__" not in p.parts]

    # Then real modules are found, so a broken glob cannot make the guard pass by
    # scanning nothing
    assert len(sources) > 10


def test_the_pattern_matches_the_real_write_site() -> None:
    # Given the one module that legitimately writes the client identity
    body = (PROJECT_ROOT / LEGITIMATE_WRITE_SITE).read_text(encoding="utf-8")

    # Then the pattern sees it. This is the assertion the previous guard failed: its
    # literal matched nothing at all, in this file or any other, so it could never fire.
    assert CLIENT_ID_WRITE.search(_without_comments(body)) is not None


@pytest.mark.parametrize(
    "idiom",
    [
        FORGERY,
        'cursor.execute("SELECT set_config(%s, %s, true)", ["app.client_id", value])',
        "cursor.execute(\"SELECT set_config('app.client_id', %s, true)\", [value])",
        "current_client_id.set(request.GET['client_id'])",
        'client_context(request.GET["client_id"])',
    ],
)
def test_the_pattern_sees_every_way_the_identity_can_be_written(idiom: str) -> None:
    # Given each shape a write can take, including the bound-parameter house idiom and
    # the exported wrapper the previous guard was blind to
    # Then all are matched
    assert CLIENT_ID_WRITE.search(idiom) is not None, idiom


def test_a_write_outside_the_permitted_modules_is_flagged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a view that writes the client identity from a query parameter, in the
    # project's own bound-parameter style
    intruder = tmp_path / "apps" / "obligations"
    intruder.mkdir(parents=True)
    (intruder / "views.py").write_text(f"def leak(request):\n    {FORGERY}\n")
    module = "tests.portal.test_client_id_provenance_guard"
    monkeypatch.setattr(f"{module}.APPS_ROOT", tmp_path / "apps")
    monkeypatch.setattr(f"{module}.PROJECT_ROOT", tmp_path)

    # Then the guard reports it. Without this the module would only ever assert that a
    # clean tree is clean, which a guard that cannot fire also satisfies.
    assert _offenders()


def test_no_module_outside_the_permitted_two_writes_the_client_identity() -> None:
    # Given every module in apps/
    offenders = _offenders()

    # Then none writes app.client_id. The identity comes from membership.client_id in
    # PortalMiddleware and from nowhere a caller can influence.
    assert not offenders, (
        "app.client_id is written outside "
        + ", ".join(sorted(PERMITTED))
        + ". Whoever writes it chooses which client's rows the session sees:\n  "
        + "\n  ".join(offenders)
    )
