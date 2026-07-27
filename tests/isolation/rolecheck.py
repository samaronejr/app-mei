"""The guard that makes every assertion in this package mean something.

`SET ROLE` MASKS the login role, so mutating the *connection* to a superuser cannot
turn this suite red — `current_user` would still report `app_runtime` and policies
would still apply. What genuinely breaks isolation is losing the `SET ROLE` itself, at
which point assertions run as `app_test`, which owns the tables and holds `BYPASSRLS`
and therefore sees every tenant's rows while every denial test still "passes".

Two independent mechanisms drop it, which is why this is asserted per test rather than
once per session:

* `TransactionTestCase` closes every initialized connection after every test, so a
  session-scoped fixture is dead from test #2 onward.
* `SET ROLE` issued inside a transaction is reverted by `ROLLBACK`, and several tests
  here force rollbacks.

This lives in a plain module, not in a fixture, on purpose: removing the `SET ROLE`
fixture must still leave the assertion running.
"""

from django.db import connection

EXPECTED_ROLE = "app_runtime"


def assert_isolated_role() -> tuple[str, str]:
    """Assert the effective role cannot bypass row-level security.

    Returns `(current_user, session_user)` so the login identity is visible in
    evidence, even though it is deliberately not what governs policy evaluation.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT current_user, session_user,
                   (SELECT rolsuper FROM pg_roles WHERE rolname = current_user),
                   (SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user)
            """,
        )
        row = cursor.fetchone()
    assert row is not None
    current_user, session_user, is_superuser, bypasses_rls = row

    assert (current_user, is_superuser, bypasses_rls) == (
        EXPECTED_ROLE,
        False,
        False,
    ), (
        f"isolation assertions must run as {EXPECTED_ROLE} with no privilege escape, "
        f"but current_user={current_user!r} rolsuper={is_superuser!r} "
        f"rolbypassrls={bypasses_rls!r} (session_user={session_user!r}). "
        f"Without this the denial tests pass while reading every tenant's rows."
    )
    return str(current_user), str(session_user)
