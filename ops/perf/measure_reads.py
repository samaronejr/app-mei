"""Bootstrap the local benchmark before importing its Django model readers.

Run with ``python -m ops.perf.measure_reads`` inside ops/perf/compose.yml.
"""

import os
import runpy

import django

from ops.perf.configuration import require_local_targets


def main() -> None:
    """Reject remote targets before Django setup, then run the initialized harness."""
    require_local_targets()
    os.environ["DJANGO_SETTINGS_MODULE"] = "ops.perf.settings"
    django.setup()
    runpy.run_module("ops.perf.runner", run_name="__main__")


if __name__ == "__main__":
    main()
