"""Bootstrap the opt-in real-HTTP mobile capture; never start a server here."""

import os
import runpy

import django

from ops.perf.configuration import require_local_targets


def main() -> None:
    """Admit only isolated local targets, then load the initialized driver."""
    require_local_targets()
    os.environ["DJANGO_SETTINGS_MODULE"] = "ops.perf.settings"
    django.setup()
    runpy.run_module("tests.perf.interaction_runner", run_name="__main__")


if __name__ == "__main__":
    main()
