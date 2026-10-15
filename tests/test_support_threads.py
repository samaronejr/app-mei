"""`run_in_thread_as_runtime` runs as the policed role, in the named tenant.

The last case is the one that matters: a raw count across two firms. Under the
login role (`app_test`, BYPASSRLS) it would read both firms' rows; under the role
the helper promises it reads one. A helper that forgot `SET ROLE` would pass every
other assertion here and fail that one.
"""

import threading

import pytest
from django.db import connection

from tests.support_threads import ThreadDidNotFinish, run_in_thread_as_runtime
from tests.ui.factories import make_firm

pytestmark = pytest.mark.django_db(transaction=True)


def _session_facts() -> tuple[str, str, int]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_user, current_setting('app.tenant_id', true), "
            "pg_backend_pid()",
        )
        role, tenant, pid = cursor.fetchone()
    return role, tenant, pid


def test_the_worker_runs_as_app_runtime_inside_the_tenant_on_its_own_session() -> None:
    firm = make_firm("fio-um")
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        (main_pid,) = cursor.fetchone()

    role, tenant, worker_pid = run_in_thread_as_runtime(
        firm.tenant.id,
        _session_facts,
    )

    assert role == "app_runtime"
    assert tenant == str(firm.tenant.id)
    assert worker_pid != main_pid, "the worker reused the test thread's session"


def test_the_worker_sees_only_its_own_tenant_through_row_level_security() -> None:
    mine = make_firm("fio-dois", client_count=1)
    make_firm("fio-tres", client_count=1)

    def raw_count() -> int:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM clients_clientcompany")
            (count,) = cursor.fetchone()
        return int(count)

    assert run_in_thread_as_runtime(mine.tenant.id, raw_count) == 1


def test_an_error_in_the_worker_is_raised_on_the_calling_thread() -> None:
    firm = make_firm("fio-quatro")

    def fail() -> None:
        msg = "planted"
        raise LookupError(msg)

    with pytest.raises(LookupError, match="planted"):
        run_in_thread_as_runtime(firm.tenant.id, fail)


def test_a_worker_that_never_finishes_fails_instead_of_hanging() -> None:
    firm = make_firm("fio-cinco")
    release = threading.Event()

    try:
        # The timeout is the behaviour under test: a blocked worker must surface.
        with pytest.raises(ThreadDidNotFinish):
            run_in_thread_as_runtime(firm.tenant.id, release.wait, timeout=0.5)
    finally:
        release.set()
