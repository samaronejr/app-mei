"""App configuration for the obligation engine."""

from django.apps import AppConfig


class ObligationsConfig(AppConfig):
    """Effective-dated fiscal parameters, due-date rules, and generated obligations."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.obligations"
