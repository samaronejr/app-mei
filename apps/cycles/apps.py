"""App configuration for the service-cycle context (S6.1)."""

from django.apps import AppConfig


class CyclesConfig(AppConfig):
    """Playbooks today; service cycles, tasks and the timeline land with T9+."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.cycles"

    def ready(self) -> None:
        """Import the signal receivers for their registration side effect."""
        # Imported inside ready(), not at module scope: the receivers reference
        # models, and importing models before the app registry is populated
        # raises AppRegistryNotReady.
        from apps.cycles import (  # noqa: F401, PLC0415
            dependents,
            export_tasks,
            retention_tasks,
            signals,
        )
        from apps.cycles.evidence_index import index_current  # noqa: PLC0415
        from apps.cycles.services.cycle import COMPLETION_GUARDS  # noqa: PLC0415

        COMPLETION_GUARDS["evidence_index"] = index_current
        from apps.cycles.extraction.service import (  # noqa: PLC0415
            EXCEPTION_REASON as WRONG_COMPANY,
        )
        from apps.cycles.openers import REASONS  # noqa: PLC0415
        from apps.cycles.scanning import EXCEPTION_REASON  # noqa: PLC0415
        from apps.cycles.services.exceptions import (  # noqa: PLC0415
            ExceptionReason,
            register_exception_reason,
        )

        # T12: the scanner stayed unavailable for 24 h after an upload.
        register_exception_reason(
            EXCEPTION_REASON,
            ExceptionReason(
                "Arquivo sem verificação antivírus. Confira o verificador ou peça "
                "um novo envio; baixe somente com o aviso de risco.",
                label="arquivo sem verificação antivírus",
            ),
        )
        # T13 (S15 B): the extracted CNPJ is not the client's. The document stays
        # with the client that sent it, suspended; T14 owns the exception screen.
        register_exception_reason(
            WRONG_COMPANY,
            ExceptionReason(
                "Documento enviado parece ser de outra empresa. Confira o CNPJ na "
                "revisão e peça ao cliente o arquivo correto; nunca reatribua o "
                "documento a outro cliente.",
                label="documento de outra empresa",
            ),
        )
        # T14: the deterministic openers the daily sweep runs.
        for code, reason in REASONS.items():
            register_exception_reason(code, reason)
        register_exception_reason(
            "approval_invalidated",
            ExceptionReason(
                "Os efeitos ou a autoridade mudaram. Confira o cartão congelado "
                "e crie uma nova versão antes de pedir outra aprovação.",
                label="aprovação invalidada",
            ),
        )
