"""App configuration for the obligation engine."""

from django.apps import AppConfig


class ObligationsConfig(AppConfig):
    """Effective-dated fiscal parameters, due-date rules, and generated obligations."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.obligations"

    def ready(self) -> None:
        """Register fiscal findings with the existing owned-exception service."""
        from apps.cycles.services.exceptions import (  # noqa: PLC0415
            ExceptionReason,
            register_exception_reason,
        )

        register_exception_reason(
            "documento_fiscal_divergente",
            ExceptionReason(
                "Confira a identidade fiscal e o papel do cliente no XML.",
                label="documento fiscal divergente",
            ),
        )
        register_exception_reason(
            "incidente_consistencia_fiscal",
            ExceptionReason(
                "Confira as revisoes contraditorias com o provedor antes de revisar.",
                label="inconsistencia fiscal",
            ),
        )
