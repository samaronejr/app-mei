"""No module outside `apps/authz/` may decide anything by comparing a role.

The whole value of holding the matrix as data is that changing a grant is a data edit.
A single `if membership.role == "owner"` in a view puts a second, invisible copy of the
matrix in the codebase — one that keeps answering the old way after the row changes,
and that no test of the matrix can reach.

The rule is mechanical: role comparisons live in `apps/authz/`, everything else asks
`can()`. The scan is an **AST walk rather than a regular expression**, and that is not
fastidiousness: the first draft used a regex and a docstring reading "who holds which
role in which firm" matched it. A guard that cries wolf on prose gets narrowed until it
stops finding anything, which is worse than never having written it.
"""

import ast
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
APPS_ROOT: Final[Path] = PROJECT_ROOT / "apps"
AUTHZ_ROOT: Final[Path] = APPS_ROOT / "authz"

ROLE_FILTER_KEYWORD: Final[str] = "role__in"
COMPARISON_OPERATORS: Final[tuple[type[ast.cmpop], ...]] = (
    ast.Eq,
    ast.NotEq,
    ast.In,
    ast.NotIn,
)


def _names_the_role(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "role"
    return isinstance(node, ast.Attribute) and node.attr == "role"


def _is_role_comparison(node: ast.Compare) -> bool:
    operands = [node.left, *node.comparators]
    return any(_names_the_role(operand) for operand in operands) and any(
        isinstance(operator, COMPARISON_OPERATORS) for operator in node.ops
    )


def _role_check_lines(source: str) -> list[int]:
    """Return the line numbers where a role is compared or filtered on."""
    tree = ast.parse(source)
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and _is_role_comparison(node):
            lines.append(node.lineno)
        elif isinstance(node, ast.keyword) and node.arg == ROLE_FILTER_KEYWORD:
            lines.append(node.value.lineno)
    return sorted(lines)


def _guarded_sources() -> list[Path]:
    return [
        path
        for path in APPS_ROOT.rglob("*.py")
        if AUTHZ_ROOT not in path.parents
        and "migrations" not in path.parts
        and "__pycache__" not in path.parts
    ]


def _offenders() -> list[str]:
    found: list[str] = []
    for path in _guarded_sources():
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        found.extend(
            f"{path.relative_to(PROJECT_ROOT)}:{number}: {lines[number - 1].strip()}"
            for number in _role_check_lines(source)
        )
    return found


def test_the_scan_actually_reaches_the_application_code() -> None:
    # Given the apps tree with apps/authz excluded
    sources = _guarded_sources()

    # When it is enumerated
    # Then real modules are found, and the invite module in particular — it held the
    # interim role gate this guard was written to reject, so a glob that missed it
    # would make the guard pass by scanning nothing that ever offended
    assert len(sources) > 10
    assert any(path.name == "invites.py" for path in sources)
    assert not any(AUTHZ_ROOT in path.parents for path in sources)


def test_the_detector_recognises_every_shape_of_the_mistake() -> None:
    # Given the ways a role gate gets written
    # When each is analysed
    # Then all are caught. Without this the guard could pass because the detector is
    # broken rather than because the codebase is clean.
    assert _role_check_lines('if membership.role == "owner":\n    pass\n') == [1]
    assert _role_check_lines("Membership.objects.filter(role__in=MAY_INVITE)\n") == [1]
    assert _role_check_lines("if role in ADMIN_ROLES:\n    pass\n") == [1]
    assert _role_check_lines('if user.role != "staff":\n    pass\n') == [1]


def test_the_detector_does_not_fire_on_prose_or_on_declarations() -> None:
    # Given a docstring that happens to contain the words, and a field declaration
    # When both are analysed
    # Then neither is reported. This is the regression the AST rewrite exists for.
    assert _role_check_lines('"""Browse who holds which role in which firm."""\n') == []
    assert _role_check_lines('role = models.CharField("role", max_length=32)\n') == []
    assert _role_check_lines("Membership.objects.create(user=u, role=r)\n") == []


def test_no_module_outside_authz_decides_anything_from_a_role() -> None:
    # Given every application module except apps/authz
    # When each is analysed for a role comparison
    offenders = _offenders()

    # Then none is found — every gate goes through can() instead
    assert not offenders, (
        "role comparisons outside apps/authz/. The permission matrix is data; "
        "gate on apps.authz.services.can() instead:\n  " + "\n  ".join(offenders)
    )
