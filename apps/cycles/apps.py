"""App configuration for the service-cycle context (S6.1)."""

from django.apps import AppConfig


class CyclesConfig(AppConfig):
    """Playbooks today; service cycles, tasks and the timeline land with T9+."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.cycles"

    def ready(self) -> None:
        """Import the signal receivers for their registration side effect."""
        # Imported inside ready(), not at module scope: the receivers reference
        # models, and importing models before the app registry is populated
        # raises AppRegistryNotReady.
        from apps.cycles import signals  # noqa: F401, PLC0415
