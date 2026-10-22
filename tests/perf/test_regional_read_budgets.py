"""Regional sections join the workspace and cycle detail inside the S11.2 budgets.

AXRM-004 baseline: each named read renders a populated client-period
(`tests/perf/regional_world.py`) within the unchanged 25-query and 150,000 gzip
byte limits. Later regional todos add their rows to that world and their
witness to `RegionalWorld.assert_populated`; the limits themselves never move.
"""

import gzip
import json
import sys
from pathlib import Path

import pytest
from pytest_django.fixtures import DjangoAssertNumQueries, SettingsWrapper

from ops.perf.results import GZIP_BYTES, QUERIES
from tests.perf.regional_world import REGIONAL_READS, regional_world

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("read", REGIONAL_READS)
def test_regional_read_stays_within_budget(
    settings: SettingsWrapper,
    tmp_path: Path,
    django_assert_max_num_queries: DjangoAssertNumQueries,
    read: str,
) -> None:
    # Given a seeded client-period populated beyond the query budget in rows.
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    settings.MEDIA_ROOT = str(tmp_path)
    world = regional_world()
    browser = world.firm()
    # When the firm owner opens the read cold, with no warmup request.
    with django_assert_max_num_queries(QUERIES) as queries:
        response = browser.get(world.path(read))
    # Then every populated row renders, and payload stays inside the budget too.
    assert response.status_code == 200, response.status_code
    world.assert_populated(read, response)
    size = len(gzip.compress(response.content, mtime=0))
    sys.stdout.write(
        "\nAXRM_REGIONAL_READ="
        + json.dumps(
            {"read": read, "queries": len(queries), "gzip_html_bytes": size},
            sort_keys=True,
        )
        + "\n"
    )
    assert size <= GZIP_BYTES, size
