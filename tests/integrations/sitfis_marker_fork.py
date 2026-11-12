"""Run the writer-capability fork proof in a fresh, single-threaded interpreter."""

import json
import os
import sys
import threading
from tempfile import TemporaryDirectory
from uuid import UUID

from django.test import override_settings

from apps.integrations import provider_journal
from apps.integrations.call_unopened import _issue_unopened_write
from apps.integrations.journal_schema import JournalError, decode


def main() -> None:
    """Keep a parent capability live while its fork attempts the public writer."""
    raw = sys.stdin.buffer.read()
    record = decode(raw, UUID(json.loads(raw)["tenant_id"]))
    if threading.active_count() != 1:
        message = "fork_proof_requires_single_thread"
        raise RuntimeError(message)
    with (
        TemporaryDirectory() as directory,
        override_settings(
            STORAGES={
                "default": {
                    "BACKEND": "django.core.files.storage.FileSystemStorage",
                    "OPTIONS": {"location": directory},
                }
            }
        ),
        _issue_unopened_write(record),
    ):
        pid = os.fork()
        if pid == 0:
            try:
                provider_journal.append(record)
            except JournalError:
                os._exit(0)
            os._exit(23)
        _, status = os.waitpid(pid, 0)
        if os.waitstatus_to_exitcode(status) != 0:
            message = "fork_reused_parent_writer_capability"
            raise AssertionError(message)
        # The rejected child cannot spend the original parent's capability.
        provider_journal.append(record)
        try:
            provider_journal.append(record)
        except JournalError:
            pass
        else:
            message = "parent_writer_capability_was_not_one_shot"
            raise AssertionError(message)
    sys.stdout.write("FORKED_MARKER_REJECTED_PARENT_CAPABILITY_CONSUMED_ONCE\n")
