"""App configuration for the accounts package."""

from django.apps import AppConfig


class AccountsConfig(AppConfig):
    """Hosts the swappable user model."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounts"
