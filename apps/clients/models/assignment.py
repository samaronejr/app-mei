"""Who inside the firm looks after which client.

The composite foreign key in this module's migration is the load-bearing part, and it
exists because of a documented PostgreSQL behaviour that defeats the obvious defence:
**referential-integrity checks bypass row security.** A plain `client_id` FK therefore
proves only that the referenced row exists *somewhere in the table*, not that it
belongs to the inserting tenant — so a `WITH CHECK` policy asserting `tenant_id =
current` accepts an assignment pointing at another firm's client. That is both a
data-integrity hole and an existence oracle, since the insert succeeds or fails
depending on whether the guessed id is real.

`FOREIGN KEY (tenant_id, client_id) REFERENCES clients_clientcompany (tenant_id, id)`
closes it structurally: the pair must exist together, so a cross-tenant reference is
not merely invisible, it is unrepresentable.
"""

from collections.abc import Iterable
from typing import ClassVar

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.base import ModelBase
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantScopedModel
from apps.tenants.models import Membership


class AssignmentRole(models.TextChoices):
    """How the account relates to the client it is assigned to."""

    PRIMARY = "primary", _("primary")
    SUPPORT = "support", _("support")


class ClientAssignment(TenantScopedModel):
    """One account's responsibility for one client."""

    client = models.ForeignKey(
        "clients.ClientCompany",
        on_delete=models.CASCADE,
        related_name="assignments",
        verbose_name=_("client"),
    )
    # A foreign key to accounts_user, which is a GLOBAL table. The composite trick
    # used for `client` is unavailable here because a global table has no tenant
    # column to pair with, and the FK check bypasses row security just the same. So
    # this one is validated in the application layer, in save() below.
    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="client_assignments",
        verbose_name=_("user"),
    )
    role = models.CharField(
        _("role"),
        max_length=16,
        choices=AssignmentRole.choices,
        default=AssignmentRole.PRIMARY,
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        """Model metadata."""

        verbose_name = _("client assignment")
        verbose_name_plural = _("client assignments")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # `tenant` leads although it is functionally determined by `client` — the
            # composite foreign key guarantees the two agree — because the index this
            # constraint creates must lead with tenant_id like every other index on a
            # policed table, or it stops serving the row-level-security predicate.
            models.UniqueConstraint(
                fields=["tenant", "client", "user"],
                name="clientassignment_tenant_client_user_uniq",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["tenant", "user"], name="assignment_tenant_user_idx"),
        ]

    def __str__(self) -> str:
        """Identify the assignment by who holds it and over what."""
        return f"{self.user_id} -> {self.client_id} ({self.role})"

    def assert_user_is_on_the_firms_roster(self) -> None:
        """Refuse an assignment to an account outside this firm's roster.

        `accounts_user` carries no tenant column, so no composite key can express
        this and the foreign-key check would accept any UUID naming a real account
        anywhere on the platform. Without this, one firm could attach a stranger to
        its client and — because `assigned_to` is what the `limited` permission level
        consults — hand them a working view of that client's data.
        """
        # PLATFORM_QUERY_OK: keyed on this row's own user and tenant. The query IS
        # the authorization check, so routing it through for_user would be circular.
        belongs = Membership.objects.filter(
            user_id=self.user_id,
            tenant_id=self.tenant_id,
            is_active=True,
        ).exists()
        if not belongs:
            msg = (
                "A client may only be assigned to an account with an active "
                "membership in the same firm."
            )
            raise ValidationError({"user": msg})

    def clean(self) -> None:
        """Surface the roster check as a form error in the admin."""
        super().clean()
        self.assert_user_is_on_the_firms_roster()

    def save(
        self,
        *,
        force_insert: bool | tuple[ModelBase, ...] = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        """Enforce the roster check on every write, not only on validated forms.

        Deliberately here rather than only in `clean`: `clean` runs for ModelForms and
        nothing else, so a management command, a data migration or a future API would
        write straight past it. This check is a tenant boundary, and a boundary that
        holds on only one code path is not a boundary.
        """
        self.assert_user_is_on_the_firms_roster()
        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )
