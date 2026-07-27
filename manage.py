#!/usr/bin/env python
"""Django command-line entrypoint."""

import os
import sys

from django.core.management import execute_from_command_line


def main() -> None:
    """Run administrative tasks against the dev settings by default."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
