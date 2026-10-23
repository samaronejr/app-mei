"""The published permission matrix, transcribed cell for cell.

Source: `deep-research-report.md` lines 51-80 — thirty capability rows by six role
columns, one hundred and eighty cells. Line 49 is the table header and line 50 its
separator; counting those two gives the wrong total.

`ROLE_ORDER` reproduces the report's column order exactly so a row here can be read
side by side with the published table. The matrix test does not trust this file: it
parses the report itself and compares the parsed cells against the seeded database,
which is why an error introduced *here* still turns the build red.
"""

from dataclasses import dataclass
from typing import Final

from apps.authz.models import GrantLevel, Role

ROLE_ORDER: Final[tuple[str, ...]] = (
    Role.PLATFORM_ADMIN,
    Role.OWNER,
    Role.STAFF_ACCOUNTANT,
    Role.OPERATIONS_ADMIN,
    Role.CLIENT_OWNER,
    Role.CLIENT_COLLABORATOR,
)

# The exact strings the report's cells contain, mapped to the enum. Keys are compared
# case-insensitively after whitespace collapsing, because a markdown table is written
# for humans and "Own actions only" may be spaced differently than it reads.
MARKER_TO_LEVEL: Final[dict[str, str]] = {
    "✅": GrantLevel.FULL,
    "❌": GrantLevel.NONE,
    "limited": GrantLevel.LIMITED,
    "view / confirm": GrantLevel.VIEW_CONFIRM,
    "initiate / approve": GrantLevel.INITIATE_APPROVE,
    "own actions only": GrantLevel.OWN,
    "ticket only": GrantLevel.TICKET_ONLY,
}

FULL = GrantLevel.FULL
NONE = GrantLevel.NONE
LIMITED = GrantLevel.LIMITED
VIEW_CONFIRM = GrantLevel.VIEW_CONFIRM
INITIATE_APPROVE = GrantLevel.INITIATE_APPROVE
OWN = GrantLevel.OWN
TICKET_ONLY = GrantLevel.TICKET_ONLY


@dataclass(frozen=True, slots=True)
class CapabilityRow:
    """One capability and its six levels, in `ROLE_ORDER`."""

    slug: str
    label: str
    levels: tuple[str, ...]

    def grants(self) -> dict[str, str]:
        """Pair each level with the role it belongs to."""
        return dict(zip(ROLE_ORDER, self.levels, strict=True))


MATRIX: Final[tuple[CapabilityRow, ...]] = (
    CapabilityRow(
        "tenants.create",
        "Create firm tenant",
        (FULL, NONE, NONE, NONE, NONE, NONE),
    ),
    CapabilityRow(
        "billing.manage",
        "Manage firm billing and plan",
        (FULL, FULL, NONE, FULL, NONE, NONE),
    ),
    CapabilityRow(
        "users.create",
        "Create and deactivate users",
        (FULL, FULL, NONE, FULL, NONE, NONE),
    ),
    CapabilityRow(
        "clients.create",
        "Create client workspace",
        (FULL, FULL, FULL, FULL, NONE, NONE),
    ),
    CapabilityRow(
        "clients.view_all",
        "View all clients in firm",
        (FULL, FULL, FULL, FULL, NONE, NONE),
    ),
    CapabilityRow(
        "clients.view_assigned",
        "View assigned clients only",
        (FULL, FULL, FULL, FULL, FULL, FULL),
    ),
    CapabilityRow(
        "clients.edit_tax_profile",
        "Edit client tax profile",
        (FULL, FULL, FULL, LIMITED, NONE, NONE),
    ),
    CapabilityRow(
        "integrations.connect",
        "Connect official integrations",
        (FULL, FULL, FULL, LIMITED, INITIATE_APPROVE, NONE),
    ),
    CapabilityRow(
        "documents.transfer",
        "Upload/download documents",
        (FULL, FULL, FULL, FULL, FULL, FULL),
    ),
    CapabilityRow(
        "das.generate",
        "Generate DAS workflows",
        (FULL, FULL, FULL, NONE, VIEW_CONFIRM, NONE),
    ),
    CapabilityRow(
        "dasn.submit",
        "Submit DASN workflow",
        (FULL, FULL, FULL, NONE, VIEW_CONFIRM, NONE),
    ),
    CapabilityRow(
        "invoices.issue",
        "Issue / request invoices",
        (FULL, FULL, FULL, NONE, FULL, LIMITED),
    ),
    CapabilityRow(
        "reports.view_financial",
        "View financial reports",
        (FULL, FULL, FULL, LIMITED, FULL, LIMITED),
    ),
    CapabilityRow(
        "permissions.manage",
        "Manage permissions",
        (FULL, FULL, NONE, LIMITED, NONE, NONE),
    ),
    CapabilityRow(
        "audit.view",
        "Access audit logs",
        (FULL, FULL, LIMITED, LIMITED, OWN, OWN),
    ),
    CapabilityRow(
        "support.access",
        "Access support / incident console",
        (FULL, FULL, LIMITED, FULL, TICKET_ONLY, TICKET_ONLY),
    ),
    # A COLLECTION capability, deliberately: the revenue-threshold queue lists every
    # client at or past the warning band, so there is no single object for a refined
    # level to be evaluated against. `reports.view_financial` is object-refined and is
    # LIMITED for operations_admin, which makes `require_can` -- which passes no object
    # -- an unconditional 403 for that role. Gating on client visibility instead would
    # collapse the authorization gate into the portfolio queryset scoping.
    CapabilityRow(
        "obligations.view_revenue_threshold_queue",
        "View revenue threshold queue",
        (FULL, FULL, FULL, FULL, NONE, NONE),
    ),
    # The R1 rows below are the S8.1 additions to the published table, and they use
    # FULL and NONE ONLY, deliberately. `require_can` passes no object, so any
    # object-refined level — LIMITED, OWN — is an unconditional denial, and the
    # channel levels never grant an action at all. Per-client narrowing is not a
    # capability level here: it happens in views and services, which load the
    # client through `visible_clients`/`load_visible_client` and answer 404.
    CapabilityRow(
        "clients.import",
        "Import client portfolio",
        (NONE, FULL, NONE, FULL, NONE, NONE),
    ),
    CapabilityRow(
        "clients.manage",
        "Maintain clients, engagements, contacts",
        (NONE, FULL, FULL, FULL, NONE, NONE),
    ),
    CapabilityRow(
        "playbooks.manage",
        "Edit and publish playbooks",
        (NONE, FULL, NONE, FULL, NONE, NONE),
    ),
    CapabilityRow(
        "cycles.operate",
        "Operate service cycles",
        (NONE, FULL, FULL, FULL, NONE, NONE),
    ),
    CapabilityRow(
        "cycles.review",
        "Review client documents",
        (NONE, FULL, FULL, FULL, NONE, NONE),
    ),
    # The "reviewer" capability. `staff_accountant` is NONE here by role and is
    # raised to FULL per person by an active `UserCapabilityGrant` — approver and
    # initiator stay different people without adding a sixth role to the matrix.
    CapabilityRow(
        "actions.approve",
        "Approve client-facing effects",
        (NONE, FULL, NONE, FULL, NONE, NONE),
    ),
    CapabilityRow(
        "cycles.complete",
        "Complete service cycles",
        (NONE, FULL, FULL, FULL, NONE, NONE),
    ),
    CapabilityRow(
        "cycles.reopen",
        "Reopen completed cycles",
        (NONE, FULL, NONE, FULL, NONE, NONE),
    ),
    CapabilityRow(
        "bulk.run",
        "Run bulk operations",
        (NONE, FULL, NONE, FULL, NONE, NONE),
    ),
    CapabilityRow(
        "automations.manage",
        "Manage automations",
        (NONE, FULL, NONE, FULL, NONE, NONE),
    ),
    CapabilityRow(
        "evidence.export",
        "Export evidence bundles",
        (NONE, FULL, FULL, FULL, NONE, NONE),
    ),
    CapabilityRow(
        "support.grant",
        "Grant support access",
        (NONE, FULL, NONE, NONE, NONE, NONE),
    ),
    # The only new capability FULL for the client roles, which is the property
    # `core.E010` demands of anything gating a portal view: `require_can` passes
    # no object, so a lesser level would be an unconditional 403 for the very
    # users the portal exists to serve.
    CapabilityRow(
        "publications.acknowledge",
        "Acknowledge publications",
        (NONE, NONE, NONE, NONE, FULL, FULL),
    ),
    CapabilityRow(
        "tenants.account",
        "Export and cancel firm account, view estimated costs",
        (NONE, FULL, NONE, NONE, NONE, NONE),
    ),
    # AXRM-007: the regional MVP rows of plan A7.6, FULL/NONE only for the same
    # reason as the R1 rows above. Seeded by authz.0009.
    CapabilityRow(
        "fiscal.connections.manage",
        "Manage fiscal provider connections",
        (NONE, FULL, NONE, FULL, NONE, NONE),
    ),
    CapabilityRow(
        "fiscal.authority.manage",
        "Manage client fiscal authority",
        (NONE, FULL, NONE, FULL, NONE, NONE),
    ),
    CapabilityRow(
        "fiscal.checks.run",
        "Run federal fiscal checks",
        (NONE, FULL, FULL, FULL, NONE, NONE),
    ),
    CapabilityRow(
        "municipal.rules.manage",
        "Author municipal rules",
        (NONE, FULL, FULL, FULL, NONE, NONE),
    ),
    CapabilityRow(
        "municipal.rules.review",
        "Review municipal rules",
        (NONE, FULL, NONE, FULL, NONE, NONE),
    ),
)

CAPABILITY_SLUGS: Final[frozenset[str]] = frozenset(row.slug for row in MATRIX)

__all__ = [
    "CAPABILITY_SLUGS",
    "MARKER_TO_LEVEL",
    "MATRIX",
    "ROLE_ORDER",
    "CapabilityRow",
]
