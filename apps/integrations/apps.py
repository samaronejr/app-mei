"""App configuration for provider connections, links and their guards."""

from django.apps import AppConfig
from django.core.checks import register


class IntegrationsConfig(AppConfig):
    """Own provider connections and client links; register the settings guard."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.integrations"

    def ready(self) -> None:
        """Attach the provider settings guard to `manage.py check` and startup."""
        from apps.integrations.checks import check_provider_settings  # noqa: PLC0415

        register(check_provider_settings)
