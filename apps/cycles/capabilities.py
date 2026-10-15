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

# Live S8.1 matrix rows seeded by AXR1-004; no legacy stand-ins.
CYCLES_OPERATE: Final = "cycles.operate"
CYCLES_REVIEW: Final = "cycles.review"
CYCLES_COMPLETE: Final = "cycles.complete"
CYCLES_REOPEN: Final = "cycles.reopen"

__all__ = [
    "CYCLES_COMPLETE",
    "CYCLES_OPERATE",
    "CYCLES_REOPEN",
    "CYCLES_REVIEW",
    "PLAYBOOK_CAPABILITY",
    "PLAYBOOK_GATE",
]
