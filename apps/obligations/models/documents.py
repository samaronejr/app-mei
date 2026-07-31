"""Fiscal evidence a client uploads, stored by reference rather than by content.

The row is metadata; the bytes live in object storage under `storage_key`. That split is
what lets every isolation control this product has apply to documents at all — a row is
something row-level security can confine, and an object in a bucket is not.

**`client` is NOT NULL, and that is a control rather than tidiness.** PostgreSQL's
default `MATCH SIMPLE` skips a composite foreign key check *entirely* when any key
column
is NULL, so a nullable `client_id` would leave the client-paired constraints present and
doing nothing. The column being mandatory is what makes those constraints load-bearing.

**`storage_key` is random and carries no authorisation meaning.** Nothing may be
inferred
from it and no access decision may rest on it being hard to guess: the download path
authorises against this row, under row-level security, before it asks storage for
anything. Unguessability is defence in depth and never the control — a key that leaks is
then a key that opens nothing.

**`obligation` is nullable on purpose.** A client may upload evidence that attaches to a
specific obligation, and may also upload a document that stands alone. The composite
foreign key on the attached case still binds, because `MATCH SIMPLE` skips only when a
key column is NULL — which for a standalone upload means there is no reference to check.
"""

import secrets
from typing import ClassVar

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantScopedModel

STORAGE_KEY_BYTES = 32
SHA256_HEX_LENGTH = 64


def new_storage_key() -> str:
    """Return an unguessable object key, drawn from the CSPRNG.

    `secrets`, not `random`: the key is not a secret in the sense that anything is
    authorised by holding it, but a predictable key would let a caller enumerate what
    exists in the bucket, and existence is itself a disclosure across clients.
    """
    return secrets.token_urlsafe(STORAGE_KEY_BYTES)


class Document(TenantScopedModel):
    """One uploaded file, owned by exactly one client."""

    client = models.ForeignKey(
        "clients.ClientCompany",
        on_delete=models.CASCADE,
        related_name="documents",
        verbose_name=_("client"),
    )
    obligation = models.ForeignKey(
        "obligations.Obligation",
        on_delete=models.CASCADE,
        related_name="documents",
        null=True,
        blank=True,
        verbose_name=_("obligation"),
    )
    storage_key = models.CharField(
        _("storage key"),
        max_length=128,
        default=new_storage_key,
        editable=False,
    )
    sha256 = models.CharField(_("sha256"), max_length=SHA256_HEX_LENGTH)
    original_filename = models.CharField(_("original filename"), max_length=255)
    content_type = models.CharField(_("content type"), max_length=127)
    byte_size = models.PositiveBigIntegerField(_("byte size"))
    uploaded_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.PROTECT,
        related_name="uploaded_documents",
        verbose_name=_("uploaded by"),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        """Model metadata."""

        verbose_name = _("document")
        verbose_name_plural = _("documents")
        ordering: ClassVar[list[str]] = ["-created_at"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # client leads. A unique constraint keyed on (tenant, storage_key) alone
            # would answer "does this key exist anywhere in the firm", which is a
            # one-bit existence oracle across clients.
            models.UniqueConstraint(
                fields=["tenant", "client", "storage_key"],
                name="document_tenant_client_storage_key_uniq",
            ),
            # Content-hash uniqueness is scoped to ONE client on purpose. Firm-wide, a
            # collision would prove that another client holds a byte-identical document
            # — which is exactly the cross-client disclosure the portal exists to
            # prevent, delivered through a constraint violation.
            models.UniqueConstraint(
                fields=["tenant", "client", "sha256"],
                name="document_tenant_client_sha256_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(byte_size__gt=0),
                name="document_byte_size_positive",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            # tenant leads, so the row-level-security predicate stays index-served.
            models.Index(
                fields=["tenant", "client", "created_at"],
                name="doc_tenant_client_created_idx",
            ),
        ]

    def __str__(self) -> str:
        """Identify the document by the name its uploader gave it."""
        return self.original_filename
