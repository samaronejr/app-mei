"""Retention vocabulary shared by existing content stores, not fiscal advice."""

from django.db import models


class RetentionClass(models.TextChoices):
    """Periods remain provisional until the owner closes OG-08."""

    FISCAL_EVIDENCE = "fiscal_evidence", "Evidência fiscal"
    OPERATIONAL = "operational", "Registro operacional"
    COMMUNICATION_BODY = "communication_body", "Conteúdo de comunicação"
    AI_DERIVED = "ai_derived", "Conteúdo derivado por IA"
    ACCESS_LOG = "access_log", "Registro de acesso"
