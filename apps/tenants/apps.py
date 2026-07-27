"""App configuration for the tenancy root."""

from django.apps import AppConfig


class TenantsConfig(AppConfig):
    """Hosts the firm, its memberships, and its pending invitations."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.tenants"
