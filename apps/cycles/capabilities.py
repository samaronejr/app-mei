"""The capability that gates playbook management, and the one used meanwhile.

`PLAYBOOK_CAPABILITY` is the slug S8.1's permissions matrix names for this
surface. T4 lands it — and its data migration — on a sibling branch, so on this
branch's base the row does not exist and asking `can()` about it would raise
`UnknownCapability` rather than refuse. `PLAYBOOK_GATE` is therefore the
capability views and services actually ask: the matrix member when it exists,
`users.create` (the closest owner/operations-admin FULL, staff NONE cell) while
it does not.

The rebase step that stacks this branch on T4 needs one edit: delete the
fallback and point `PLAYBOOK_GATE` at `PLAYBOOK_CAPABILITY`.
"""

from typing import Final

from apps.authz.matrix import CAPABILITY_SLUGS

PLAYBOOK_CAPABILITY: Final = "playbooks.manage"

# The interim gate. owners and operations admins manage playbooks; staff
# accountants do not — matching the column S8.1 defines for `playbooks.manage`.
PLAYBOOK_GATE: Final = (
    PLAYBOOK_CAPABILITY if PLAYBOOK_CAPABILITY in CAPABILITY_SLUGS else "users.create"
)

__all__ = ["PLAYBOOK_CAPABILITY", "PLAYBOOK_GATE"]
