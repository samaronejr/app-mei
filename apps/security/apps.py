"""App configuration for request-level hardening."""

from django.apps import AppConfig


class SecurityConfig(AppConfig):
    """Hold the rate-limiting middleware and its policy."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.security"
