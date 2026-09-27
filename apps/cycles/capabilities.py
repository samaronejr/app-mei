"""The capability that gates playbook management: S8.1's `playbooks.manage`.

T4 seeded the matrix row (`(NONE, FULL, NONE, FULL, NONE, NONE)` — owners and
operations admins manage, staff accountants and client roles do not), so the
slug is live below this branch and views/services ask it directly through
`PLAYBOOK_GATE`. The constant pair stays as one import seam: every consumer
asks `PLAYBOOK_GATE`, which is `PLAYBOOK_CAPABILITY`.
"""

from typing import Final

PLAYBOOK_CAPABILITY: Final = "playbooks.manage"
PLAYBOOK_GATE: Final = PLAYBOOK_CAPABILITY

__all__ = ["PLAYBOOK_CAPABILITY", "PLAYBOOK_GATE"]
