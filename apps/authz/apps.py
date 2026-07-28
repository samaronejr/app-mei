"""App configuration for authorization."""

from django.apps import AppConfig


class AuthzConfig(AppConfig):
    """The permission matrix, held as rows rather than as branches in view code."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.authz"
