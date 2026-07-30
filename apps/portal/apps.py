"""App configuration for the client portal."""

from django.apps import AppConfig


class PortalConfig(AppConfig):
    """The MEI client portal, served on its own host."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.portal"
