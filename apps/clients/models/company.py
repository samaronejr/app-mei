"""The client registry: the MEI companies one accounting firm looks after.

Two properties of this table are load-bearing and neither is obvious.

**CNPJ uniqueness is per tenant, never global.** Two firms legitimately serve the same
MEI — during a hand-over both hold the client at once, and a global unique index would
let firm A's registry decide whether firm B may onboard a company. That is both a
denial of service across a tenant boundary and an existence oracle: a rejected insert
tells the caller the number exists somewhere else in the platform.

**`(tenant, id)` is unique even though `id` alone already is.** PostgreSQL documents
that referential-integrity checks bypass row security, so a foreign key from a
tenant-scoped child to this table proves only that the row exists *somewhere*. Every
such child therefore carries a composite `FOREIGN KEY (tenant_id, client_id)` pointing
at this constraint, which makes a cross-tenant reference structurally impossible rather
than merely invisible. The constraint is that FK's referent, so it must exist before
any child table is created.
"""

from typing import ClassVar

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.clients.managers import ClientCompanyManager
from apps.clients.models.onboarding import OnboardingStatus
from apps.core.models import TenantScopedModel

# Both documents are stored normalized: uppercase, punctuation stripped, fixed width.
CNPJ_LENGTH = 14
CPF_LENGTH = 11

# Fixed width is expressed as a CHECK over a varchar rather than as a `char(14)`.
# `char(n)` pads a short value with spaces instead of rejecting it, so a normalizer
# that ever produced 13 characters would silently store a wrong number that still
# compares equal to itself. The check refuses it loudly, which is what the anchor's
# "stored normalized CHAR(14)" is actually asking for.
#
# The class is `[0-9A-Z]`, not `[0-9]`: IN RFB nº 2.229/2024 makes CNPJ alphanumeric
# for registrations from 2026-07-31, so a digits-only constraint would have to be
# migrated away before the product's first alphanumeric client. Check-digit validation
# is a separate concern and arrives with the validators in T-032/T-033.
CNPJ_SHAPE = r"^[0-9A-Z]{14}$"
CPF_SHAPE = r"^[0-9A-Z]{11}$"


class ClientStatus(models.TextChoices):
    """Where a client sits in its life with the firm."""

    ONBOARDING = "onboarding", _("onboarding")
    ACTIVE = "active", _("active")
    SUSPENDED = "suspended", _("suspended")
    CLOSED = "closed", _("closed")


class ClientCompany(TenantScopedModel):
    """One MEI company on one firm's books."""

    legal_name = models.CharField(_("legal name"), max_length=255)
    trade_name = models.CharField(_("trade name"), max_length=255, blank=True)
    cnpj = models.CharField(_("CNPJ"), max_length=CNPJ_LENGTH)
    # DJ001 warns against a nullable string field because it creates two spellings of
    # "empty", NULL and "". That is answered here rather than ignored: the check
    # constraint below admits NULL or exactly eleven normalized characters and nothing
    # in between, so "" is rejected by the database and only one empty exists.
    cpf = models.CharField(_("CPF"), max_length=CPF_LENGTH, null=True, blank=True)  # noqa: DJ001
    municipality_ibge_code = models.CharField(
        _("IBGE municipality code"),
        max_length=7,
        blank=True,
    )
    state = models.CharField(_("state"), max_length=2, blank=True)
    main_cnae = models.CharField(_("main CNAE"), max_length=7, blank=True)
    opened_on = models.DateField(_("opened on"), null=True, blank=True)
    status = models.CharField(
        _("status"),
        max_length=20,
        choices=ClientStatus.choices,
        default=ClientStatus.ONBOARDING,
    )
    is_mei = models.BooleanField(_("MEI"), default=True)
    # Payroll is out of scope for v1, but the flag sizes the demand for it and keeps
    # a later eSocial decision a data question rather than a migration.
    has_employee = models.BooleanField(_("has employee"), default=False)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    # Shadows the inherited scoped manager by name, which is what keeps
    # Meta.default_manager_name pointing at something that still filters by tenant.
    objects: ClassVar[ClientCompanyManager] = ClientCompanyManager()

    class Meta(TenantScopedModel.Meta):
        """Model metadata."""

        verbose_name = _("client company")
        verbose_name_plural = _("client companies")
        ordering: ClassVar[list[str]] = ["legal_name"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # Per tenant. A global unique index here would let one firm's registry
            # veto another firm's onboarding, and leak that the number exists.
            models.UniqueConstraint(
                fields=["tenant", "cnpj"],
                name="clientcompany_tenant_cnpj_uniq",
            ),
            # Redundant against the primary key on its own, and deliberately so: it
            # is the referent every composite tenant-crossing FK points at.
            models.UniqueConstraint(
                fields=["tenant", "id"],
                name="clientcompany_tenant_id_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(cnpj__regex=CNPJ_SHAPE),
                name="clientcompany_cnpj_normalized",
            ),
            models.CheckConstraint(
                condition=models.Q(cpf__isnull=True) | models.Q(cpf__regex=CPF_SHAPE),
                name="clientcompany_cpf_normalized",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            # tenant_id leads. The row-level-security predicate filters on it, so an
            # index that does not lead with it stops serving the predicate and every
            # scan degrades to a sequential one under the policy.
            models.Index(fields=["tenant", "status"], name="client_tenant_status_idx"),
        ]

    def __str__(self) -> str:
        """Identify the client by the name it is registered under."""
        return self.legal_name

    @property
    def is_ready(self) -> bool:
        """Report whether every applicable onboarding item is settled.

        A client with no checklist at all answers `False` rather than `True`. The
        vacuous reading — "nothing outstanding, therefore ready" — would report a
        client nobody has assessed as fit to file for, which is the one wrong answer
        that costs a firm a deadline.
        """
        statuses = set(self.onboarding_items.values_list("status", flat=True))
        if not statuses:
            return False
        return not (statuses & OnboardingStatus.unfinished())
