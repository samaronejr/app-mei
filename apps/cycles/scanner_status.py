"""The scanner's own pulse (AXRM-005): clamd `PING` + `VERSION`, stamped for the page.

`ping_scanner` (beat) calls `check_scanner` with the configured adapter. The answer
lands on the platform-level `scanner` heartbeat row, the same table and columns the
Automações page already reads for every job: `last_attempt_at` on every run,
`last_success_at` and the signature database version only when clamd answered both
commands. A failed run keeps the last success and its version, so the page shows
how stale the scanner is, not a blank.

Never a readiness signal: `/readyz` does not read this row or call clamd. A scanner
outage fails closed in intake (files wait in quarantine, or are refused under
`scan_required` while scanning is switched off); restarting the web process would
fix nothing.
"""

from datetime import datetime
from typing import Final

from apps.core.tasks import assert_no_transaction
from apps.cycles.scanning import ClamdScanner, ScannerUnavailable
from apps.obligations.models import SchedulerHeartbeat

SCANNER_HEARTBEAT: Final = "scanner"
OK: Final = "ok"
DISABLED_CODE: Final = "clamav_desativado"
UNAVAILABLE_CODE: Final = "scanner_indisponivel"


def check_scanner(scanner: ClamdScanner | None, *, now: datetime) -> str:
    """Ask clamd PING then VERSION (I/O outside any transaction) and stamp the row."""
    assert_no_transaction()
    code, outcomes = DISABLED_CODE, None
    if scanner is not None:
        try:
            scanner.ping()
            engine, signatures = scanner.version()
        except ScannerUnavailable:
            code = UNAVAILABLE_CODE
        else:
            code, outcomes = "", {"engine": engine, "signature_version": signatures}
    stamp, _ = SchedulerHeartbeat.objects.get_or_create(name=SCANNER_HEARTBEAT)
    stamp.last_attempt_at = now
    stamp.last_error_code = code
    fields = ["last_attempt_at", "last_error_code", "updated_at"]
    if outcomes is not None:
        stamp.last_success_at = now
        stamp.outcomes_json = outcomes
        fields += ["last_success_at", "outcomes_json"]
    stamp.save(update_fields=fields)
    return code or OK
