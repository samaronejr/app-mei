"""App configuration for the fiscal primitives."""

from django.apps import AppConfig


class FiscalConfig(AppConfig):
    """Document validators and the municipality capability registry."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.fiscal"
