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


# Modules that name a role to constrain DATA SHAPE rather than to decide permission.
#
# The rule this guard enforces is that no second copy of the permission matrix exists
# outside apps/authz. A CHECK constraint saying "a client role names a client and a firm
# role does not" is not that: it grants nothing, denies nothing, and stays correct when
# a RoleGrant level changes. It decides which COLUMNS a row may fill.
#
# The rule cannot live in apps/authz, because apps/authz/portfolio.py already imports
# apps.tenants.models and the dependency would be circular.
#
# Pinned to a literal, with an exact expected count per file, so this is an escape hatch
# that cannot silently widen: adding one more role comparison to an exempt module fails
# the count assertion even though the module is listed here.
ROLE_SHAPE_EXEMPT: Final[dict[str, str]] = {
    "apps/tenants/models.py": (
        "membership_role_matches_client_scope and invite_role_is_firm_side are CHECK "
        "constraints on row shape, and firm_role_choices restricts which roles an "
        "invitation may offer so accepting one cannot violate the first constraint. "
        "None of the three decides what a role may do."
    ),
}

# The exact role references the exemption buys, pinned to their SOURCE TEXT.
#
# An earlier version pinned the COUNT (`== 4`). That left the exemption open to the one
# edit it most needed to catch: delete a shape check and add a permission gate in the
# same module, and the count is still four while a second copy of the matrix has just
# been born inside the exempt file. Text catches the swap; a count cannot see it.
#
# Line numbers are deliberately NOT pinned. They shift on any edit above, and a guard
# that fails for reasons unrelated to what it protects is one that gets deleted.
#
#   firm_role_choices          -- which roles an invitation may offer
#   membership CHECK, arm 1    -- a firm role has no client
#   membership CHECK, arm 2    -- a client role names one
#   invite CHECK, arm 1        -- a firm-side invitation names no client
#   invite CHECK, arm 2        -- a portal invitation names the client it is for
#
# The last entry was `condition=models.Q(role__in=FIRM_ROLES),` until W7 opened. While
# portal invitations were gated, an invitation could only ever be firm-side, and that
# single arm was the whole rule; the absence of `Invite.client` was itself the evidence
# that the gated feature had not been pre-built. W7's approved schema gives the
# invitation the same optional client the membership it creates carries, so the CHECK
# becomes the same two-armed rule. The pin is RETARGETED, not relaxed: five exact lines,
# still character-for-character, still refusing a sixth.
#
# The two CHECKs now read identically, and that is the finding rather than an oversight
# — they are one rule evaluated one step apart. Nothing is lost by the repetition: a
# permission gate never spells itself `models.Q(role__in=..., client__isnull=...)`, so
# the swap this pin exists to catch still cannot hide inside a duplicate.
EXEMPT_ROLE_CHECK_SOURCE: Final[dict[str, tuple[str, ...]]] = {
    "apps/tenants/models.py": (
        (
            "return [(role.value, str(role.label)) for role in TenantRole "
            "if role in FIRM_ROLES]"
        ),
        "models.Q(role__in=FIRM_ROLES, client__isnull=True)",
        "| models.Q(role__in=CLIENT_ROLES, client__isnull=False)",
        "models.Q(role__in=FIRM_ROLES, client__isnull=True)",
        "| models.Q(role__in=CLIENT_ROLES, client__isnull=False)",
    ),
}


def _role_checks_by_file() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in _guarded_sources():
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        hits = [
            f"{path.relative_to(PROJECT_ROOT)}:{number}: {lines[number - 1].strip()}"
            for number in _role_check_lines(source)
        ]
        if hits:
            found[str(path.relative_to(PROJECT_ROOT))] = hits
    return found


def _role_check_texts(module: str) -> tuple[str, ...]:
    source = (PROJECT_ROOT / module).read_text(encoding="utf-8")
    lines = source.splitlines()
    return tuple(lines[number - 1].strip() for number in _role_check_lines(source))


def _offenders() -> list[str]:
    return [
        hit
        for name, hits in _role_checks_by_file().items()
        if name not in ROLE_SHAPE_EXEMPT
        for hit in hits
    ]


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


def test_the_shape_exemption_is_pinned_to_a_literal() -> None:
    # Given the exemption list
    # When its keys are compared to the pinned-source map
    # Then they are the same set. A module listed in one and not the other is either an
    # unjustified exemption or an unpinned one, and both defeat the guard.
    assert set(ROLE_SHAPE_EXEMPT) == set(EXEMPT_ROLE_CHECK_SOURCE)


def test_every_shape_exemption_carries_a_justification() -> None:
    # Given the exemption list
    # Then each entry says why, in prose a reviewer can disagree with
    for module, reason in ROLE_SHAPE_EXEMPT.items():
        assert reason.strip(), module


def test_an_exempt_module_holds_exactly_the_role_checks_it_was_exempted_for() -> None:
    # Given the modules exempted for data-shape reasons
    for module, expected in EXEMPT_ROLE_CHECK_SOURCE.items():
        # When their role references are read back out of the source
        actual = _role_check_texts(module)

        # Then each is character-for-character what was argued for. Being on the list
        # buys those four lines and nothing else: a fifth is a new decision that has to
        # be argued for rather than inherited, and REPLACING one is caught too, which
        # is the case a count was blind to.
        assert actual == expected, (
            f"{module}'s role references are not the ones it was exempted for.\n"
            f"If the change also constrains row shape, update the pin deliberately; "
            f"if it decides permission, move it behind can().\n"
            f"  expected: {list(expected)}\n"
            f"  actual:   {list(actual)}"
        )


def test_the_pin_catches_a_swap_that_leaves_the_count_unchanged() -> None:
    # Given the exempt module's real role references
    real = _role_check_texts("apps/tenants/models.py")

    # And a forgery that drops one shape check and adds a permission gate
    forged = (*real[:-1], 'if membership.role == "owner":')

    # Then the count is identical, so the previous version of this guard saw nothing
    assert len(forged) == len(real)

    # But the pinned source differs, so this version fails. Without this test the
    # exemption would still be a hole and the module above would pass either way.
    assert forged != real


def test_no_module_outside_authz_decides_anything_from_a_role() -> None:
    # Given every application module except apps/authz
    # When each is analysed for a role comparison
    offenders = _offenders()

    # Then none is found — every gate goes through can() instead
    assert not offenders, (
        "role comparisons outside apps/authz/. The permission matrix is data; "
        "gate on apps.authz.services.can() instead:\n  " + "\n  ".join(offenders)
    )
