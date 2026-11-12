"""Client fiscal identities and the immutable source revisions behind their heads."""

from typing import ClassVar

from django.db import models
from django.utils import timezone

from apps.core.models import TenantScopedModel


class FiscalDocument(TenantScopedModel):
    """One identity; its mutable projection is pinned to an immutable revision."""

    client = models.ForeignKey("clients.ClientCompany", on_delete=models.PROTECT)
    environment = models.CharField(max_length=16)
    family = models.CharField(
        max_length=24,
        choices=[
            (value, value) for value in ("nfe", "nfse_nacional", "nfse_municipal")
        ],
    )
    key_namespace = models.CharField(max_length=64)
    document_key = models.CharField(max_length=128)
    client_role = models.CharField(
        max_length=16,
        choices=[
            (value, value)
            for value in ("destinatario", "emitente", "tomador", "prestador")
        ],
    )
    counterpart_id = models.CharField(max_length=14, blank=True)
    counterpart_name = models.CharField(max_length=255, blank=True)
    issue_date = models.DateField(null=True)
    competence = models.DateField(null=True)
    municipality_incidence = models.CharField(max_length=7, blank=True)
    total = models.DecimalField(max_digits=14, decimal_places=2, null=True)
    iss_stated = models.DecimalField(max_digits=14, decimal_places=2, null=True)
    iss_withheld_flag = models.BooleanField(null=True)
    iss_withheld_amount = models.DecimalField(
        max_digits=14, decimal_places=2, null=True
    )
    current_revision = models.ForeignKey(
        "obligations.FiscalRevision",
        on_delete=models.PROTECT,
        null=True,
        related_name="+",
    )
    summary_only = models.BooleanField(default=True)
    source = models.CharField(
        max_length=24,
        choices=[
            (value, value)
            for value in ("captura", "upload_cliente", "importacao_equipe")
        ],
    )
    provider = models.CharField(max_length=32, blank=True)
    fiscal_state = models.CharField(max_length=16, default="desconhecida")
    role_verification = models.CharField(
        max_length=16,
        default="unconfirmed",
        choices=[(value, value) for value in ("unconfirmed", "confirmed", "divergent")],
    )
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(TenantScopedModel.Meta):
        """Tenant-led identity and period lookup."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["tenant", "client", "id"], name="fiscaldocument_scope_id_uniq"
            ),
            models.UniqueConstraint(
                fields=[
                    "tenant",
                    "client",
                    "environment",
                    "family",
                    "key_namespace",
                    "document_key",
                ],
                name="fiscaldocument_identity_uniq",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["tenant", "client", "competence"],
                name="fiscaldocument_period_idx",
            ),
        ]


class FiscalRevision(TenantScopedModel):
    """One provider snapshot; corrections append rather than rewriting evidence."""

    client = models.ForeignKey("clients.ClientCompany", on_delete=models.PROTECT)
    document = models.ForeignKey(
        FiscalDocument, on_delete=models.PROTECT, related_name="revisions"
    )
    source_namespace = models.CharField(max_length=64)
    provider_version = models.CharField(max_length=64)
    semantic_digest = models.CharField(max_length=64)
    fiscal_state = models.CharField(
        max_length=16,
        choices=[
            (value, value)
            for value in (
                "autorizada",
                "cancelada",
                "substituida",
                "denegada",
                "desconhecida",
            )
        ],
    )
    source_effective_at = models.DateTimeField(null=True)
    observed_at = models.DateTimeField(default=timezone.now)
    replacement_key = models.CharField(max_length=128, blank=True)
    receipt_ref = models.CharField(max_length=128, blank=True)
    provenance = models.JSONField(default=dict)
    version = models.PositiveIntegerField(default=1)

    class Meta(TenantScopedModel.Meta):
        """A source version belongs to one client and fiscal identity."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["tenant", "client", "id"], name="fiscalrevision_scope_id_uniq"
            ),
            models.UniqueConstraint(
                fields=[
                    "tenant",
                    "client",
                    "document",
                    "source_namespace",
                    "provider_version",
                ],
                name="fiscalrevision_source_version_uniq",
            ),
            models.UniqueConstraint(
                fields=["tenant", "client", "document", "id"],
                name="fiscalrevision_document_id_uniq",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["tenant", "client", "document"], name="fiscalrevision_doc_idx"
            ),
        ]


class RevisionArtifact(TenantScopedModel):
    """An immutable association of retained vault bytes with a fiscal snapshot."""

    client = models.ForeignKey("clients.ClientCompany", on_delete=models.PROTECT)
    revision = models.ForeignKey(
        FiscalRevision, on_delete=models.PROTECT, related_name="artifacts"
    )
    role = models.CharField(
        max_length=16,
        choices=[
            (value, value) for value in ("resumo", "xml_original", "xml_cce", "pdf")
        ],
    )
    document = models.ForeignKey(
        "obligations.Document",
        on_delete=models.PROTECT,
        related_name="fiscal_artifacts",
    )
    version = models.PositiveIntegerField(default=1)

    class Meta(TenantScopedModel.Meta):
        """Retained bytes are unique within an exact revision and role."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["tenant", "client", "id"], name="revisionartifact_scope_id_uniq"
            ),
            models.UniqueConstraint(
                fields=["tenant", "client", "revision", "role", "document"],
                name="revisionartifact_bytes_uniq",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["tenant", "client", "revision"], name="revisionartifact_rev_idx"
            ),
        ]
