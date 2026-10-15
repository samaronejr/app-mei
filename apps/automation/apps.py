"""No admin, views or user-visible AI surface."""

from django.apps import AppConfig


class AutomationConfig(AppConfig):
    """Register private shadow records only."""

    name = "apps.automation"

    def ready(self) -> None:
        """Protect imported clients once private processing records exist."""
        from apps.automation import dependents  # noqa: F401, PLC0415
