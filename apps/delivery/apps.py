"""Durable SMTP delivery, private to the firm's operational surface."""

from django.apps import AppConfig


class DeliveryConfig(AppConfig):
    """Own the outbox and its deterministic integrations."""

    name = "apps.delivery"

    def ready(self) -> None:
        """Register deterministic exception openers and the timeline reader."""
        from apps.cycles.services.exceptions import (  # noqa: PLC0415
            ExceptionReason,
            register_exception_reason,
        )
        from apps.cycles.workspace import OUTBOX_TIMELINE_READERS  # noqa: PLC0415
        from apps.delivery.timeline import read_timeline  # noqa: PLC0415

        register_exception_reason(
            "delivery_unknown",
            ExceptionReason(
                "Verifique o envio antes de decidir; "
                "não tente reenviar automaticamente."
            ),
        )
        register_exception_reason(
            "delivery_rejected",
            ExceptionReason("Revise o contato e a recusa do servidor de e-mail."),
        )
        OUTBOX_TIMELINE_READERS.append(read_timeline)
