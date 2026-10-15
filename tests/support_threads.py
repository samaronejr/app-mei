"""Run tenant-scoped work on a second thread, under the role production runs as.

Race tests need two database sessions at once: the test's own, and another that
contends with it for a row lock, a version check or a unique constraint. A thread
gets a fresh Django connection -- connections are thread-local -- but that
connection logs in as `app_test`, which owns every table and holds BYPASSRLS
(`ops/sql/roles.sql`). `tests/conftest.py` switches only the test thread's own
connection to `app_runtime`, so work done on a bare thread would read and write
every tenant's rows and a race test would pass while proving nothing about the
policies it runs under.

`run_in_thread_as_runtime` closes that gap: its thread issues `SET ROLE
app_runtime` before anything else, enters `tenant_context` for the tenant named,
runs the callable, and closes the thread's connections on the way out so nothing
holds a lock into the next test's flush.

The second session cannot see the test thread's uncommitted rows. Tests using
this need `django_db(transaction=True)`, which commits as it goes.
"""

import threading
from collections.abc import Callable
from typing import Final
from uuid import UUID

from django.db import connection, connections

from apps.core.tenancy import tenant_context

RUNTIME_ROLE: Final = "app_runtime"

# Generous for a statement or two against a local database; exceeded only by a
# thread that is blocked -- which is exactly when a test must fail rather than hang.
DEFAULT_TIMEOUT_SECONDS: Final = 30.0


class ThreadDidNotFinish(TimeoutError):  # noqa: N818
    """The worker thread was still running when the timeout expired.

    Usually a lock the test thread still holds. Raised instead of joining forever,
    so a deadlocked race test fails with a reason rather than stalling the suite.
    """


def run_in_thread_as_runtime[T](
    tenant_id: UUID,
    fn: Callable[[], T],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> T:
    """Call `fn` on a new thread as `app_runtime` inside `tenant_context(tenant_id)`.

    Returns what `fn` returns. An exception raised by `fn` -- or by the role switch
    or the context itself -- is re-raised here, on the calling thread, so the test
    sees the real error rather than a missing result.
    """
    results: list[T] = []
    errors: list[BaseException] = []

    def work() -> None:
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"SET ROLE {RUNTIME_ROLE}")
            with tenant_context(tenant_id):
                results.append(fn())
        except BaseException as error:  # noqa: BLE001 - re-raised on the caller's thread
            errors.append(error)
        finally:
            connections.close_all()

    worker = threading.Thread(target=work, name=f"runtime-{tenant_id}", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        msg = (
            f"the {RUNTIME_ROLE} worker for tenant {tenant_id} did not finish within "
            f"{timeout}s; the test thread is probably holding a lock it waits on"
        )
        raise ThreadDidNotFinish(msg)
    if errors:
        raise errors[0]
    return results[0]


__all__ = ["ThreadDidNotFinish", "run_in_thread_as_runtime"]
