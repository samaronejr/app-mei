"""AXRM-019 repair 4 witness: setup's post-write re-read holds the link's row lock.

Review 4 showed the re-read's row lock is observable: when a disable's local stop has
updated the row but not committed, the locked re-read waits and sees the disable (the
setup compensates and refuses), while a plain read sees `ativa` and the setup reports
ok. This pins the property deterministically: while the re-read's transaction is
open, nobody else can lock the row.
"""

import os
from collections.abc import Callable

import psycopg
import pytest
from django.db import connection

from apps.integrations.focus import company as focus_company
from apps.integrations.focus.company import SetupPlan
from apps.integrations.models import ClientProviderLink
from tests.integrations.test_focus_adapter import World, world
from tests.integrations.test_focus_adapter_edges import NFSEN, _setup, outcome_of

__all__ = ["world"]
db = pytest.mark.django_db(transaction=True)


@db
def test_the_post_write_reread_holds_the_link_row_lock(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    held: list[bool] = []
    original = focus_company._reread_locked

    def probe(
        execute: Callable[..., object],
        sql: str,
        params: object,
        many: bool,
        context: dict[str, object],
    ) -> object:
        result = execute(sql, params, many, context)
        if "integrations_clientproviderlink" in sql:
            # The superuser URL names the lane database; pytest runs in its test DB.
            with psycopg.connect(
                os.environ["SUPERUSER_DATABASE_URL"],
                dbname=connection.settings_dict["NAME"],
            ) as other:
                try:
                    other.execute(
                        "SELECT 1 FROM integrations_clientproviderlink "
                        "WHERE id = %s FOR UPDATE NOWAIT",
                        [world.link.pk],
                    )
                except psycopg.errors.LockNotAvailable:
                    held.append(True)
                else:
                    held.append(False)
                other.rollback()
        return result

    def reread(plan: SetupPlan) -> ClientProviderLink:
        with connection.execute_wrapper(probe):
            return original(plan)

    monkeypatch.setattr(focus_company, "_reread_locked", reread)
    assert outcome_of(_setup(world, *NFSEN)) == "ok"
    assert held == [True]
