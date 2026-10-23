"""Startup guards for the provider kill switches (plan A11, ADR-0008).

`integrations.E001`: a provider environment set to `producao` while
`PROVIDER_REAL_DATA_APPROVED` is False and `PROVIDERS_PAUSED` is False -- the one
combination that would let a deploy reach real taxpayer data without the owner's
approval. The claim path (todo 9) refuses such calls independently; this makes the
misconfiguration a startup error instead of a refusal discovered per call.

`integrations.E002`: an environment name outside the four known ones. Without it a
misspelt `producao` would read as "not production" and pass E001 silently.
"""

from collections.abc import Sequence
from typing import Any, Final

from django.apps.config import AppConfig
from django.conf import settings
from django.core.checks import CheckMessage, Error

from apps.integrations.models import ProviderEnvironment

ENVIRONMENT_SETTINGS: Final = ("SERPRO_ENVIRONMENT", "FOCUS_ENVIRONMENT")


def check_provider_settings(
    app_configs: Sequence[AppConfig] | None,  # noqa: ARG001
    **kwargs: Any,  # noqa: ANN401, ARG001
) -> list[CheckMessage]:
    """Refuse an unknown environment, or production without approval or pause."""
    errors: list[CheckMessage] = []
    known = set(ProviderEnvironment.values)
    for name in ENVIRONMENT_SETTINGS:
        value = getattr(settings, name, None)
        if value not in known:
            errors.append(
                Error(
                    f"{name}={value!r} is not a provider environment.",
                    hint="Use one of: " + ", ".join(sorted(known)) + ".",
                    id="integrations.E002",
                )
            )
        elif (
            value == ProviderEnvironment.PRODUCAO
            and settings.PROVIDER_REAL_DATA_APPROVED is not True
            and settings.PROVIDERS_PAUSED is not True
        ):
            errors.append(
                Error(
                    f"{name} is producao while PROVIDER_REAL_DATA_APPROVED is False "
                    "and PROVIDERS_PAUSED is False.",
                    hint=(
                        "Keep PROVIDERS_PAUSED=True until the owner approves real "
                        "data (OD-R3); production calls need "
                        "PROVIDER_REAL_DATA_APPROVED=True."
                    ),
                    id="integrations.E001",
                )
            )
    return errors
