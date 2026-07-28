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

from collections.abc import Collection, Iterable
from typing import ClassVar

from django.db import models
from django.db.models.base import ModelBase
from django.utils.translation import gettext_lazy as _

from apps.clients import readiness
from apps.clients.managers import ClientCompanyManager
from apps.clients.models.onboarding import OnboardingStatus
from apps.core.models import TenantScopedModel
from apps.fiscal.formatting import format_cnpj, format_cpf
from apps.fiscal.mei import MEICategory
from apps.fiscal.validators import normalize_document, validate_cnpj, validate_cpf

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


class GovBrTrustLevel(models.TextChoices):
    """The gov.br account tier, RECORDED as data and never verified here.

    No gov.br OIDC integration exists in this scope, so this column is what a person
    told the firm, not what an identity provider asserted. `UNKNOWN` is the default
    and is treated exactly like `BRONZE` by the e-CAC blocker: "nobody has asked" and
    "asked, and the answer disqualifies them" are both states in which the firm cannot
    rely on portal access.
    """

    UNKNOWN = "unknown", _("unknown")
    BRONZE = "bronze", _("bronze")
    PRATA = "prata", _("prata")
    OURO = "ouro", _("ouro")

    @classmethod
    def below_ecac(cls) -> frozenset[str]:
        """Return the tiers e-CAC refuses. e-CAC admits only prata and ouro."""
        return frozenset({cls.UNKNOWN, cls.BRONZE})


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
    cnpj = models.CharField(
        _("CNPJ"),
        max_length=CNPJ_LENGTH,
        validators=[validate_cnpj],
    )
    # DJ001 warns against a nullable string field because it creates two spellings of
    # "empty", NULL and "". That is answered here rather than ignored: the check
    # constraint below admits NULL or exactly eleven normalized characters and nothing
    # in between, so "" is rejected by the database and only one empty exists.
    cpf = models.CharField(  # noqa: DJ001
        _("CPF"),
        max_length=CPF_LENGTH,
        null=True,
        blank=True,
        validators=[validate_cpf],
    )
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
    # Which annual ceiling this client is measured against. Not derivable from the
    # CNAE: a trucker qualifies as MEI-Caminhoneiro through the transport activity
    # codes, but the registry records what the firm established, not what a code
    # table implies. Defaulted to common because the overwhelming majority are, and
    # because defaulting to the higher caminhoneiro ceiling would understate
    # consumption for everyone else — reporting an over-limit client as compliant.
    mei_category = models.CharField(
        _("MEI category"),
        max_length=16,
        choices=MEICategory.choices,
        default=MEICategory.COMMON,
    )
    govbr_trust_level = models.CharField(
        _("gov.br trust level"),
        max_length=16,
        choices=GovBrTrustLevel.choices,
        default=GovBrTrustLevel.UNKNOWN,
    )
    # Certificate METADATA only, and that boundary is enforced by a test rather than
    # by discipline: custody of a client's A1 certificate would make this product a
    # holder of signing keys for every firm on it, which the anchor forbids in v1.
    # There is deliberately no field here that could hold the certificate itself.
    has_digital_certificate = models.BooleanField(
        _("has digital certificate"),
        default=False,
    )
    certificate_expires_on = models.DateField(
        _("certificate expires on"),
        null=True,
        blank=True,
    )
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

    def normalize_documents(self) -> None:
        """Strip punctuation and uppercase the two document columns, in place."""
        self.cnpj = normalize_document(self.cnpj)
        if self.cpf:
            self.cpf = normalize_document(self.cpf)

    def clean_fields(self, exclude: Collection[str] | None = None) -> None:
        """Normalize before ANY field validation runs, not merely before `clean`.

        This must be `clean_fields` rather than `clean`. `full_clean` runs
        `clean_fields` first, and that is where `max_length` is enforced — a masked
        `12.ABC.345/01DE-35` is eighteen characters, so it is rejected for length
        before a later hook could strip the punctuation. Normalizing here puts the
        bare fourteen characters in place ahead of the length check, the check-digit
        validator, the per-tenant uniqueness check, and the database's
        `clientcompany_cnpj_normalized` CHECK, all of which then see the value that
        will actually be stored.
        """
        self.normalize_documents()
        super().clean_fields(exclude=exclude)

    def save(
        self,
        *,
        force_insert: bool | tuple[ModelBase, ...] = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        """Normalize on every write, not only on the paths that call `full_clean`.

        `clean` runs for ModelForms and nothing else, so a management command, an
        import or a future API would otherwise store a masked value that no search
        would ever match again. Uniqueness is per tenant on this column, so a second
        spelling of the same number is also a duplicate the constraint would not catch.
        """
        self.normalize_documents()
        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )

    @property
    def masked_cnpj(self) -> str:
        """Return the CNPJ in the official `AA.AAA.AAA/AAAA-DD` display mask."""
        return format_cnpj(self.cnpj)

    @property
    def masked_cpf(self) -> str:
        """Return the CPF in the official `AAA.AAA.AAA-DD` mask, or empty if unset."""
        return format_cpf(self.cpf) if self.cpf else ""

    def _checklist(self) -> list[readiness.ChecklistEntry]:
        return [
            readiness.ChecklistEntry(status=status, requires_ecac=requires_ecac)
            for status, requires_ecac in self.onboarding_items.values_list(
                "status",
                "requires_ecac",
            )
        ]

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

    @property
    def readiness_score(self) -> int:
        """Return the percentage of applicable checklist items that are done."""
        return readiness.score(self._checklist())

    @property
    def ecac_blocker(self) -> str | None:
        """Return why e-CAC is out of reach for this client, or None if it is not."""
        return readiness.ecac_blocker(
            self._checklist(),
            self.govbr_trust_level,
            GovBrTrustLevel.below_ecac(),
        )
