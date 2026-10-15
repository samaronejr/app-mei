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
        from apps.delivery.kinds import INVITE_REJECTED  # noqa: PLC0415
        from apps.delivery.timeline import read_timeline  # noqa: PLC0415

        register_exception_reason(
            "delivery_unknown",
            ExceptionReason(
                "Verifique o envio antes de decidir; "
                "não tente reenviar automaticamente.",
                label="resultado incerto do envio",
            ),
        )
        register_exception_reason(
            "delivery_rejected",
            ExceptionReason(
                "Revise o contato e a recusa do servidor de e-mail.",
                label="e-mail recusado pelo servidor",
            ),
        )
        register_exception_reason(
            INVITE_REJECTED,
            ExceptionReason(
                "O convite foi cancelado. Confira o e-mail do contato e "
                "convide de novo.",
                label="convite do portal recusado pelo servidor de e-mail",
            ),
        )
        OUTBOX_TIMELINE_READERS.append(read_timeline)

        from apps.cycles.effects.delivery import (  # noqa: PLC0415
            register as register_approved,
        )
        from apps.cycles.effects.guards import (  # noqa: PLC0415
            actions_terminal,
            effects_started,
        )
        from apps.cycles.services.cycle import (  # noqa: PLC0415
            COMPLETION_GUARDS,
            EFFECT_GUARDS,
        )
        from apps.delivery.kinds import register  # noqa: PLC0415

        register()
        register_approved()
        COMPLETION_GUARDS["actions"] = actions_terminal
        EFFECT_GUARDS["approved_actions"] = effects_started
