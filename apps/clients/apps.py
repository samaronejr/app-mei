"""App configuration for the client registry."""

from django.apps import AppConfig


class ClientsConfig(AppConfig):
    """The firm's portfolio: companies, assignments, tags, onboarding."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.clients"
