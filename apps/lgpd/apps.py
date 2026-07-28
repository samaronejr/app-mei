"""App configuration for the LGPD rights channel."""

from django.apps import AppConfig


class LgpdConfig(AppConfig):
    """Hold the data-subject-rights intake route."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.lgpd"
