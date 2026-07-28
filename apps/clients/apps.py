"""App configuration for the client registry."""

from django.apps import AppConfig


class ClientsConfig(AppConfig):
    """The firm's portfolio: companies, assignments, tags, onboarding."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.clients"

    def ready(self) -> None:
        """Import the signal receivers for their registration side effect."""
        # Imported inside ready(), not at module scope: the receivers reference
        # models, and importing models before the app registry is populated
        # raises AppRegistryNotReady.
        from apps.clients import signals  # noqa: F401, PLC0415
