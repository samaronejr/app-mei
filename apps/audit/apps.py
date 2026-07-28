"""App configuration for the audit trail."""

from django.apps import AppConfig


class AuditConfig(AppConfig):
    """Connect the identity signals that feed the platform-event stream."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.audit"

    def ready(self) -> None:
        """Import the signal receivers for their registration side effect."""
        # Imported inside ready(), not at module scope: the receivers reference
        # models, and importing models before the app registry is populated
        # raises AppRegistryNotReady.
        from apps.audit import signals  # noqa: F401, PLC0415
