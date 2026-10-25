"""App configuration for provider connections, links and their guards."""

from django.apps import AppConfig
from django.core.checks import register


class IntegrationsConfig(AppConfig):
    """Own provider connections and client links; register the settings guard."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.integrations"

    def ready(self) -> None:
        """Attach the settings guard, the issue reasons and the sweep rule."""
        from apps.cycles.openers import TENANT_EVALUATORS  # noqa: PLC0415
        from apps.cycles.services.exceptions import (  # noqa: PLC0415
            register_exception_reason,
        )
        from apps.integrations.authority import (  # noqa: PLC0415
            REASONS,
            evaluate_provider_conditions,
        )
        from apps.integrations.budget_alerts import (  # noqa: PLC0415
            REASONS as BUDGET_REASONS,
        )
        from apps.integrations.checks import check_provider_settings  # noqa: PLC0415
        from apps.integrations.serpro.registration import (  # noqa: PLC0415
            register_serpro_operations,
        )
        from apps.integrations.serpro.work import (  # noqa: PLC0415
            REASONS as SERPRO_REASONS,
        )
        from apps.integrations.serpro.work import (  # noqa: PLC0415
            evaluate_termo_conditions,
        )

        register(check_provider_settings)
        for code, reason in (REASONS | BUDGET_REASONS | SERPRO_REASONS).items():
            register_exception_reason(code, reason)
        TENANT_EVALUATORS.extend(
            (evaluate_provider_conditions, evaluate_termo_conditions)
        )
        register_serpro_operations()
