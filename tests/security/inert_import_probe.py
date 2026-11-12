"""Run the actual production console without installing a Sentry client."""

import json
import logging
import sys

import django

CANARY = "synthetic-fix6-no-dsn-credential-456291"
CALLS: list[str] = []


class Opaque:
    def __str__(self) -> str:
        CALLS.append("str")
        message = "application renderer ran"
        raise RuntimeError(message)

    def __repr__(self) -> str:
        CALLS.append("repr")
        message = "application renderer ran"
        raise RuntimeError(message)


def main() -> None:
    django.setup()
    logging.getLogger("fix6.no-dsn").error(
        "ordinary no-dsn diagnostic %s", Opaque(), extra={"password": CANARY}
    )
    sys.stdout.write(json.dumps({"calls": CALLS}))


if __name__ == "__main__":
    main()
