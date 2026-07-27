"""App configuration for the concrete fixture models."""

from django.apps import AppConfig


class CoreTestsConfig(AppConfig):
    """Registered only by config.settings.test.

    The abstract bases have no table, and the first real tenant-scoped model does not
    arrive until the client registry. Row-level security, the scoped manager, and the
    isolation suite all need something concrete to assert against before then.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core.tests"
    label = "core_tests"
