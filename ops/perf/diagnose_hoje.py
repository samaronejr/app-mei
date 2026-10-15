"""Bootstrap the opt-in, local-only Hoje SQL investigation.

Run with ``python -m ops.perf.diagnose_hoje --output PATH --head SHA``.
The populated lane must already exist; this command never seeds or analyzes it.
"""

import os
import runpy

import django

from ops.perf.configuration import require_local_targets


def main() -> None:
    """Reject unsafe targets before importing Django model readers."""
    require_local_targets()
    os.environ["DJANGO_SETTINGS_MODULE"] = "ops.perf.settings"
    django.setup()
    runpy.run_module("ops.perf.hoje_investigation", run_name="__main__")


if __name__ == "__main__":
    main()
