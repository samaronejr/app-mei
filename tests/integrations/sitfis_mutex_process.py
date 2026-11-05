"""Hold one real PostgreSQL report mutex until this isolated process dies."""

import json
import sys
from uuid import UUID

from apps.integrations.serpro.sitfis_wire import report_mutex


def main() -> None:
    """Publish readiness only after the session owns its advisory mutex."""
    data = json.loads(sys.stdin.readline())
    with report_mutex(UUID(data["tenant_id"]), UUID(data["report_id"])):
        sys.stdout.write("SITFIS_REPORT_MUTEX_OWNED\n")
        sys.stdout.flush()
        sys.stdin.readline()
