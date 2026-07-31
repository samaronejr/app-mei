"""All 102 cells, checked against the report rather than against what seeded them.

The obvious version of this test compares the database to `apps.authz.matrix.MATRIX`
and proves only that the seed ran. It would pass with a transcription error in every
single cell, which is the one failure worth catching.

So the report is parsed here directly. The database and the expectation are read from
two independent places, joined only on the capability's published wording, and a wrong
cell in `matrix.py` turns this red.
"""

import importlib
import re
from pathlib import Path
from typing import Final

import pytest
from django.apps import apps as django_apps

from apps.authz.matrix import MARKER_TO_LEVEL, MATRIX
from apps.authz.models import Capability, GrantLevel, Role, RoleGrant
from apps.core.rls import is_exempt_from_tenant_policy
from apps.tenants.models import TenantRole

REPORT: Final[Path] = Path(__file__).resolve().parents[2] / "deep-research-report.md"
HEADER_PREFIX: Final[str] = "| Capability | Platform admin |"

# The plan pins the table to lines 51-67 and warns that counting the header and the
# separator gives the wrong total. Both are asserted below rather than assumed.
EXPECTED_FIRST_DATA_LINE: Final[int] = 51
EXPECTED_LAST_DATA_LINE: Final[int] = 67
EXPECTED_CAPABILITIES: Final[int] = 17
EXPECTED_CELLS: Final[int] = 102

COLUMN_TO_ROLE: Final[dict[str, str]] = {
    "Platform admin": Role.PLATFORM_ADMIN,
    "Firm owner": Role.OWNER,
    "Staff accountant": Role.STAFF_ACCOUNTANT,
    "Firm operations admin": Role.OPERATIONS_ADMIN,
    "MEI client owner": Role.CLIENT_OWNER,
    "MEI client collaborator": Role.CLIENT_COLLABORATOR,
}


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _level(marker: str) -> str:
    # Collapse whitespace and lower-case before lookup: a markdown table is written
    # for a reader, so "Own actions only" may be spaced differently than it reads.
    key = re.sub(r"\s+", " ", marker).strip().lower()
    if key not in MARKER_TO_LEVEL:
        msg = f"unmapped marker {marker!r} in the report matrix"
        raise AssertionError(msg)
    return MARKER_TO_LEVEL[key]


def _report_table() -> tuple[int, list[str], list[str]]:
    """Return the header's 1-based line number, its columns, and the data rows."""
    lines = REPORT.read_text(encoding="utf-8").splitlines()
    header_index = next(
        index for index, line in enumerate(lines) if line.startswith(HEADER_PREFIX)
    )
    rows = []
    cursor = header_index + 2
    while cursor < len(lines) and lines[cursor].startswith("|"):
        rows.append(lines[cursor])
        cursor += 1
    return header_index + 1, _cells(lines[header_index]), rows


def _published_matrix() -> dict[tuple[str, str], str]:
    _header_line, columns, rows = _report_table()
    roles = [COLUMN_TO_ROLE[column] for column in columns[1:]]
    published = {}
    for row in rows:
        cells = _cells(row)
        label = cells[0]
        for role, marker in zip(roles, cells[1:], strict=True):
            published[(label, role)] = _level(marker)
    return published


PUBLISHED: Final[dict[tuple[str, str], str]] = _published_matrix()

EVERY_CELL = pytest.mark.parametrize(
    ("label", "role", "expected"),
    [(label, role, level) for (label, role), level in sorted(PUBLISHED.items())],
    ids=[f"{label}|{role}" for label, role in sorted(PUBLISHED)],
)


def test_the_published_table_sits_where_the_plan_says_it_does() -> None:
    # Given the research report
    header_line, columns, rows = _report_table()

    # When the table is located
    # Then it is the one the plan references, and the data rows are 51-66 — the
    # header and separator are excluded, which is the miscount the plan warns about
    assert header_line == EXPECTED_FIRST_DATA_LINE - 2
    assert len(rows) == EXPECTED_CAPABILITIES
    assert header_line + 2 == EXPECTED_FIRST_DATA_LINE
    assert header_line + 1 + len(rows) == EXPECTED_LAST_DATA_LINE
    assert columns[1:] == list(COLUMN_TO_ROLE)


def test_the_parse_produces_one_hundred_and_two_cells() -> None:
    # Given the parsed table
    # When its cells are counted
    # Then there are 16 x 6. A broken parse would make every assertion below vacuous.
    assert len(PUBLISHED) == EXPECTED_CELLS
    assert len({label for label, _role in PUBLISHED}) == EXPECTED_CAPABILITIES


@pytest.mark.django_db
@EVERY_CELL
def test_every_seeded_cell_matches_the_published_matrix(
    label: str,
    role: str,
    expected: str,
) -> None:
    # Given one cell of the published table
    capability = Capability.objects.filter(label=label).first()
    assert capability is not None, f"no capability seeded for the report row {label!r}"

    # When the seeded grant for that role is read
    grant = RoleGrant.objects.filter(capability=capability, role=role).first()

    # Then it holds exactly the published level
    assert grant is not None, f"no grant seeded for {label!r} / {role}"
    assert grant.level == expected, (
        f"cell {label!r} / {role} is seeded as {grant.level!r} but the report "
        f"publishes {expected!r}"
    )


@pytest.mark.django_db
def test_the_seed_covers_the_matrix_and_nothing_else() -> None:
    # Given the seeded tables
    # When the totals are counted
    # Then they are exactly the published matrix, with no extra rows invented
    assert Capability.objects.count() == EXPECTED_CAPABILITIES
    assert RoleGrant.objects.count() == EXPECTED_CELLS
    assert {row.slug for row in MATRIX} == set(
        Capability.objects.values_list("slug", flat=True),
    )


@pytest.mark.django_db(transaction=True)
def test_seeding_a_second_time_changes_nothing() -> None:
    # Given the matrix seeded once by the data migration's own function. It is run
    # here rather than relied upon: a transactional test starts from truncated
    # tables, so migration-created rows are not present at this point. Seeding
    # explicitly makes the test assert idempotency rather than assert the fixture.
    #
    # schema_editor is unused by seed(), which is why None is safe and this exercises
    # the real code path instead of a re-implementation of it.
    migration = importlib.import_module("apps.authz.migrations.0002_seed_matrix")
    migration.seed(django_apps, None)
    before = sorted(
        RoleGrant.objects.values_list("role", "capability__slug", "level"),
    )
    assert len(before) == EXPECTED_CELLS, "the first seed did not populate the matrix"

    # When it is replayed
    migration.seed(django_apps, None)

    # Then nothing was duplicated and nothing changed, so fixing a wrong cell is a
    # re-run rather than a hand-written repair script
    assert Capability.objects.count() == EXPECTED_CAPABILITIES
    assert RoleGrant.objects.count() == EXPECTED_CELLS
    assert (
        sorted(RoleGrant.objects.values_list("role", "capability__slug", "level"))
        == before
    )


@pytest.mark.django_db
def test_every_assignable_membership_role_is_scored_by_the_matrix() -> None:
    # Given the roles a Membership can actually hold
    assignable = {role.value for role in TenantRole}

    # When each is looked for in the matrix
    # Then all are present and fully scored. A membership role with no grants would
    # be denied everything by can(), silently, with no error to trace.
    assert assignable <= {role.value for role in Role}
    for role in assignable:
        assert RoleGrant.objects.filter(role=role).count() == EXPECTED_CAPABILITIES


@pytest.mark.django_db
def test_all_seven_levels_are_present_in_the_seeded_data() -> None:
    # Given the seven-value level enum
    # When the seeded levels are collected
    seeded = set(RoleGrant.objects.values_list("level", flat=True))

    # Then every one is used. A four-value enum could not have expressed three of
    # these, which is why the enum has seven — this asserts that claim rather than
    # restating it.
    assert seeded == {level.value for level in GrantLevel}


def test_both_authz_tables_are_declared_platform_level() -> None:
    # Given the two new tables
    # When the allow-list is consulted
    # Then both are listed, so the coverage meta-test's inverse check stays green
    # without either table quietly acquiring a tenant policy it cannot satisfy
    assert is_exempt_from_tenant_policy(Capability._meta.db_table)
    assert is_exempt_from_tenant_policy(RoleGrant._meta.db_table)
