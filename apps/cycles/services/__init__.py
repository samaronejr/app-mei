"""Domain services for the playbook lifecycle.

Every firm-side write to a playbook goes through here and carries the same four
guarantees (S6.3, worker-contract pitfalls):

* a row lock — `select_for_update` on the `Playbook` aggregate row, taken inside
  `tenant_context`, so two publishers of the same playbook serialize;
* an optimistic check — callers carry the `version` integer they read, and a
  mismatch raises `VersionConflict` before anything is written;
* a `can()` re-check — the view's decorator is not the gate a management command
  or a later API would pass, so the capability is asked again inside the lock;
* an audit row — `record_event` in the same transaction, so a refused or crashed
  write never leaves an event claiming it happened.

`actor=None` is the platform's own identity — used by `install_system_packs`,
which is the only caller allowed to mutate an `is_system` playbook. For it the
capability check is structurally inapplicable (there is no account to grant),
and the `is_system` restriction is exactly inverted: system callers may ONLY
touch system rows.
"""

from apps.cycles.services.playbooks import (
    OPEN_CYCLE_STATES,
    PublishPreview,
    clone_playbook,
    create_draft_version,
    create_playbook,
    move_step,
    preview_publish,
    publish_version,
    retire_version,
    update_draft,
)

__all__ = [
    "OPEN_CYCLE_STATES",
    "PublishPreview",
    "clone_playbook",
    "create_draft_version",
    "create_playbook",
    "move_step",
    "preview_publish",
    "publish_version",
    "retire_version",
    "update_draft",
]
