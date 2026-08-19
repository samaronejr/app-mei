"""`CSRF_TRUSTED_ORIGINS` is declared-but-empty, and three files have to agree on that.

`.env.prod.example` ships the variable blank, `config/settings/base.py` defaults it to
`[]`, and `docker-compose.prod.yml` decides whether Compose renders at all. Nothing at
runtime forces the three to agree, and the way they disagreed stayed invisible until an
operator was already mid-deploy: the compose file used `${VAR:?}`, which rejects unset
AND empty, so the shipped-and-correct `CSRF_TRUSTED_ORIGINS=` stopped PILOT-403's shadow
deploy before a single container started. The only two ways out of that error are to
delete the guard or to invent a non-empty origin, and inventing one re-admits exactly
the sibling-subdomain origins that host-only cookies exist to exclude.

`${VAR?}` keeps the half worth keeping: an ABSENT declaration still refuses, because
absent means nobody ever decided whether this deployment allows cross-origin POSTs.

These assertions run the real interpolator instead of reading the file for a `?`.
Asserting the character would restate the diff; only `docker compose config` can say
whether Compose agrees, and Compose disagreeing with the .env template is the whole
outage. The third case pins the other end: relaxing Compose must not have relaxed
Django, so a wildcard still has to fail `core.E003` by name.
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from django.test import override_settings

from apps.core.checks import check_transaction_and_cookie_policy

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROD_COMPOSE = PROJECT_ROOT / "docker-compose.prod.yml"

SUBJECT = "CSRF_TRUSTED_ORIGINS"
WILDCARD_ORIGIN = "https://*.example.dev"
THROWAWAY = "throwaway-value-that-is-not-a-credential"

# Both mandatory interpolation forms: `${NAME:?msg}` rejects unset AND empty, `${NAME?}`
# rejects only unset. The set is DERIVED rather than hardcoded so that adding a
# nineteenth required variable cannot make the absent-CSRF case below pass because
# Compose stopped on something else entirely.
MANDATORY_INTERPOLATION = re.compile(r"\$\{(?P<name>[A-Z0-9_]+):?\?")


def _executable_compose_body() -> str:
    lines = PROD_COMPOSE.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("#"))


def _mandatory_variable_names() -> set[str]:
    names: set[str] = set(MANDATORY_INTERPOLATION.findall(_executable_compose_body()))
    assert SUBJECT in names, (
        f"{SUBJECT} is no longer a mandatory interpolation in {PROD_COMPOSE.name}, so "
        f"these cases would build their environment from the wrong variable list"
    )
    assert len(names) > 1, (
        f"only {SUBJECT} was discovered; the interpolation pattern stopped matching, "
        f"so every case below would render against an empty environment"
    )
    return names


@functools.cache
def _docker_compose_cli() -> str | None:
    docker = shutil.which("docker")
    if docker is None:
        return None
    probe = subprocess.run(  # noqa: S603
        [docker, "compose", "version"],
        capture_output=True,
        check=False,
    )
    return docker if probe.returncode == 0 else None


requires_compose = pytest.mark.skipif(
    _docker_compose_cli() is None,
    reason=(
        "docker compose is absent, so the production interpolation contract cannot be "
        "exercised by effect here; CI runs on ubuntu-24.04, where it is present"
    ),
)


def _render_production_compose(
    subject_value: str | None,
) -> subprocess.CompletedProcess[str]:
    """Render the production file with every mandatory variable but the subject set.

    The environment is built explicitly and `--env-file` is pointed at the null device,
    so neither a developer's `.env` nor the ambient shell can supply the one value each
    case is about.
    """
    docker = _docker_compose_cli()
    assert docker is not None
    environment = dict.fromkeys(_mandatory_variable_names() - {SUBJECT}, THROWAWAY)
    environment["PATH"] = os.environ["PATH"]
    if subject_value is not None:
        environment[SUBJECT] = subject_value
    return subprocess.run(  # noqa: S603
        [
            docker,
            "compose",
            "--env-file",
            os.devnull,
            "--file",
            str(PROD_COMPOSE),
            "config",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=PROJECT_ROOT,
        env=environment,
    )


@requires_compose
def test_an_absent_declaration_stops_compose_before_any_container_starts() -> None:
    # Given an environment satisfying every mandatory variable except the subject
    # When Compose renders the production file
    rendered = _render_production_compose(None)

    # Then it refuses, and it refuses BY NAME. A non-zero exit alone would also be
    # produced by an unrelated missing variable, which would prove nothing about the
    # one guard this file exists to pin.
    assert rendered.returncode != 0, (
        f"an undeclared {SUBJECT} rendered successfully; the deployment would boot "
        f"with no operator decision about cross-origin POSTs at all\n{rendered.stdout}"
    )
    assert f"required variable {SUBJECT} is missing" in rendered.stderr, (
        f"Compose refused for some other reason, so this case proves nothing about "
        f"{SUBJECT}\n{rendered.stderr}"
    )


@requires_compose
def test_a_declared_empty_value_renders() -> None:
    # Given the same environment, with the subject declared and left empty as
    # .env.prod.example ships it
    # When Compose renders the production file
    rendered = _render_production_compose("")

    # Then it succeeds, and the rendered config carries the empty value through. The
    # exit code alone is not enough: it would also be 0 if Compose had dropped the key,
    # which is a different production configuration from an explicitly empty one.
    assert rendered.returncode == 0, (
        f"the shipped .env.prod.example value cannot render; this is exactly how "
        f"PILOT-403's shadow deploy failed\n{rendered.stderr}"
    )
    assert f'{SUBJECT}: ""' in rendered.stdout, (
        f"{SUBJECT} is absent from the rendered config, so Django would fall back to "
        f"its own default rather than receiving the operator's explicit empty list"
    )


@override_settings(CSRF_TRUSTED_ORIGINS=[WILDCARD_ORIGIN])
def test_a_wildcard_origin_is_still_rejected_by_core_e003() -> None:
    # Given the wildcard .env.prod.example warns about, which the relaxed Compose guard
    # now happily renders
    # When the system checks run
    errors = check_transaction_and_cookie_policy(None)

    # Then core.E003 still fires by name. Compose stopped policing the VALUE, so this
    # check is now the only thing standing between a wildcard and a running stack.
    offenders = [error for error in errors if error.id == "core.E003"]
    assert offenders, (
        f"{WILDCARD_ORIGIN} passed every check; one firm's page could post to another "
        f"firm's host with a token host-only cookies were supposed to keep out"
    )
    assert WILDCARD_ORIGIN in offenders[0].msg
