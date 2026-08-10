import ast
import inspect
from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from tests.ui.test_status_render_policy import EnumPolicy, enum_policies

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
APPS_ROOT: Final[Path] = PROJECT_ROOT / "apps"


@dataclass(frozen=True)
class LiteralHit:
    path: str
    line: int
    column: int
    value: str
    source: str

    def report(self) -> str:
        return f"{self.path}:{self.line}: quoted status literal {self.value!r}"


@dataclass(frozen=True)
class LiteralPin:
    path: str
    value: str
    source: str
    count: int = field(default=1, compare=False)


APPROVED_LITERAL_SOURCES: Final[dict[LiteralPin, str]] = {
    LiteralPin(
        "apps/accounts/models.py",
        "active",
        'is_active = models.BooleanField(_("active"), default=True)',
    ): (
        "The word is translatable field metadata for account availability, not an "
        "in-scope status value used by behavioural code."
    ),
    LiteralPin(
        "apps/authz/models.py",
        "owner",
        'OWNER = "owner", _("firm owner")',
    ): (
        "Role is the authorization matrix enum and deliberately mirrors TenantRole; "
        "its equality is pinned by the authorization suite."
    ),
    LiteralPin(
        "apps/authz/models.py",
        "staff_accountant",
        'STAFF_ACCOUNTANT = "staff_accountant", _("staff accountant")',
    ): (
        "Role is the authorization matrix enum and deliberately mirrors TenantRole; "
        "its equality is pinned by the authorization suite."
    ),
    LiteralPin(
        "apps/authz/models.py",
        "operations_admin",
        'OPERATIONS_ADMIN = "operations_admin", _("firm operations admin")',
    ): (
        "Role is the authorization matrix enum and deliberately mirrors TenantRole; "
        "its equality is pinned by the authorization suite."
    ),
    LiteralPin(
        "apps/authz/models.py",
        "client_owner",
        'CLIENT_OWNER = "client_owner", _("MEI client owner")',
    ): (
        "Role is the authorization matrix enum and deliberately mirrors TenantRole; "
        "its equality is pinned by the authorization suite."
    ),
    LiteralPin(
        "apps/authz/models.py",
        "client_collaborator",
        'CLIENT_COLLABORATOR = "client_collaborator", _("MEI client collaborator")',
    ): (
        "Role is the authorization matrix enum and deliberately mirrors TenantRole; "
        "its equality is pinned by the authorization suite."
    ),
    LiteralPin(
        "apps/clients/models/onboarding.py",
        "active",
        'is_active = models.BooleanField(_("active"), default=True)',
    ): (
        "The word is translatable metadata for enabling a checklist template, not an "
        "OnboardingStatus or ClientStatus branch."
    ),
    LiteralPin(
        "apps/core/checks.py",
        "client_owner",
        'portal_roles = ("client_owner", "client_collaborator")',
    ): (
        "The deployment check intentionally avoids model imports and verifies the two "
        "portal columns in the static authorization matrix."
    ),
    LiteralPin(
        "apps/core/checks.py",
        "client_collaborator",
        'portal_roles = ("client_owner", "client_collaborator")',
    ): (
        "The deployment check intentionally avoids model imports and verifies the two "
        "portal columns in the static authorization matrix."
    ),
    LiteralPin(
        "apps/core/views.py",
        "unknown",
        '"scheduler": "unknown",',
    ): (
        "This is an operational health response when the heartbeat store is "
        "unreadable, not a client gov.br tier."
    ),
    LiteralPin(
        "apps/core/views.py",
        "unknown",
        '"backup": "unknown",',
    ): (
        "This is an operational health response when the heartbeat store is "
        "unreadable, not a client gov.br tier."
    ),
    LiteralPin(
        "apps/fiscal/capabilities.py",
        "unknown",
        'UNKNOWN = "unknown", _("unknown")',
        count=2,
    ): (
        "CapabilityStatus is a separate enum for municipal-registry confidence; both "
        "the value and its translation msgid are declared on this pinned line."
    ),
    LiteralPin(
        "apps/obligations/heartbeat.py",
        "unknown",
        'UNKNOWN = "unknown"',
    ): (
        "DiskHeadroom is an infrastructure-health enum unrelated to the reader-facing "
        "gov.br trust-level enum."
    ),
    LiteralPin(
        "apps/obligations/queries.py",
        "overdue",
        '"overdue",',
    ): (
        "This is the public export name of the date-derived overdue query function, "
        "not an ObligationStatus comparison."
    ),
    LiteralPin(
        "apps/obligations/views.py",
        "overdue",
        '"overdue",',
    ): (
        "This is the URL-facing queue slug for the date-derived overdue view, not a "
        "stored ObligationStatus branch."
    ),
    LiteralPin(
        "apps/portal/views.py",
        "overdue",
        'return int(totals["total"]), int(totals["overdue"])',
    ): (
        "This is an aggregate alias for a date-derived counter whose query explicitly "
        "does not trust the status column."
    ),
    LiteralPin(
        "apps/security/ratelimit.py",
        "unknown",
        'UNKNOWN = "unknown"',
    ): (
        "This sentinel keys a rate-limit bucket when no submitted identity exists and "
        "is unrelated to every presentation enum."
    ),
    LiteralPin(
        "apps/tenants/models.py",
        "active",
        'is_active = models.BooleanField(_("active"), default=True)',
        count=2,
    ): (
        "These two words are translatable field metadata for enabled tenant rows and "
        "invitations, not ClientStatus values."
    ),
}


def _literal_hits(name: str, source: str, values: Collection[str]) -> list[LiteralHit]:
    lines = source.splitlines()
    return sorted(
        (
            LiteralHit(
                name,
                node.lineno,
                node.col_offset,
                node.value,
                lines[node.lineno - 1].strip(),
            )
            for node in ast.walk(ast.parse(source, filename=name))
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in values
        ),
        key=lambda hit: (hit.path, hit.line, hit.column),
    )


def _enum_values(policies: Sequence[EnumPolicy]) -> frozenset[str]:
    return frozenset(
        str(member.value) for policy in policies for member in policy.enum_class
    )


def _declaration_locations(policies: Sequence[EnumPolicy]) -> set[tuple[str, int, int]]:
    locations: set[tuple[str, int, int]] = set()
    for policy in policies:
        source_path = inspect.getsourcefile(policy.enum_class)
        assert source_path is not None, f"no source file for {policy.enum_name}"
        path = Path(source_path).resolve()
        relative = str(path.relative_to(PROJECT_ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        enum_node = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == policy.enum_name
        )
        member_names = {member.name for member in policy.enum_class}
        for statement in enum_node.body:
            targets: list[ast.expr] = []
            if isinstance(statement, ast.Assign):
                targets = statement.targets
            elif isinstance(statement, ast.AnnAssign):
                targets = [statement.target]
            if not any(
                isinstance(target, ast.Name) and target.id in member_names
                for target in targets
            ):
                continue
            locations.update(
                (relative, node.lineno, node.col_offset)
                for node in ast.walk(statement)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            )
    assert locations, "the quoted-status helper found no enum declarations to exclude"
    return locations


def _approved_pin_counts(hits: Sequence[LiteralHit]) -> Counter[LiteralPin]:
    return Counter(
        LiteralPin(hit.path, hit.value, hit.source)
        for hit in hits
        if LiteralPin(hit.path, hit.value, hit.source) in APPROVED_LITERAL_SOURCES
    )


def _application_status_literal_offenders() -> list[str]:
    sources = sorted(
        path
        for path in APPS_ROOT.rglob("*.py")
        if "migrations" not in path.parts and "__pycache__" not in path.parts
    )
    assert len(sources) > 100, "the quoted-status helper found no application tree"
    policies = enum_policies()
    values = _enum_values(policies)
    assert len(values) > 20, "the quoted-status helper found no enum member values"
    hits = [
        hit
        for path in sources
        for hit in _literal_hits(
            str(path.relative_to(PROJECT_ROOT)),
            path.read_text(encoding="utf-8"),
            values,
        )
    ]
    assert hits, "the quoted-status helper found no declaration controls"
    declarations = _declaration_locations(policies)
    approved_counts = _approved_pin_counts(hits)
    assert all(reason.strip() for reason in APPROVED_LITERAL_SOURCES.values()), (
        "every quoted-literal exemption must carry a written reason"
    )
    expected_counts = Counter({pin: pin.count for pin in APPROVED_LITERAL_SOURCES})
    assert approved_counts == expected_counts, (
        "the quoted-literal exemptions no longer match their pinned source lines:\n"
        f"  expected: {expected_counts}\n  actual: {approved_counts}"
    )
    return [
        hit.report()
        for hit in hits
        if (hit.path, hit.line, hit.column) not in declarations
        and LiteralPin(hit.path, hit.value, hit.source) not in APPROVED_LITERAL_SOURCES
    ]


def test_application_code_does_not_quote_status_member_values() -> None:
    offenders = _application_status_literal_offenders()

    assert offenders == [], "quoted status literal policy violation:\n  " + "\n  ".join(
        offenders
    )


def test_the_literal_scan_accepts_enum_references_and_reports_quotes() -> None:
    planted = (
        'current = "active"\n'
        "expected = ClientStatus.ACTIVE\n"
        'explanation = "active is a stored status"\n'
    )

    hits = _literal_hits("apps/plantado.py", planted, {"active"})

    assert [hit.report() for hit in hits] == [
        "apps/plantado.py:1: quoted status literal 'active'"
    ]
