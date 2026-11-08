"""All causal consume/exit/append orders, including real threads and forks."""

import subprocess
import sys
from dataclasses import replace

import pytest

from apps.integrations.call_unopened import UNOPENED_RESULT
from tests.integrations.sitfis_support import clock, world
from tests.integrations.test_sitfis_unopened import Veto, veto

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["clock", "veto", "world"]


def test_every_causal_scope_order_preserves_durable_authority(veto: Veto) -> None:
    veto.run()
    [dispatch] = veto.dispatches
    record = replace(dispatch.receipt, result=UNOPENED_RESULT)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import django; django.setup(); "
                "from tests.integrations.sitfis_scope_orderings import main; main()"
            ),
        ],
        input=record.encode(),
        capture_output=True,
        timeout=120,
        check=False,
    )
    sys.stdout.write(result.stdout.decode())
    assert result.returncode == 0, result.stderr.decode()
    assert b"SCOPE_ORDERINGS_CHECKED=20" in result.stdout
