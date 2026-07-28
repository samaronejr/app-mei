"""The obligation engine's data tables. The engine is data, not code.

Limits and deadlines in this domain change politically, on short notice, and often
retroactively. A ceiling written as a constant needs a deploy on the day the law
changes — which is the day the firm can least afford one — so every number the engine
reads is a row here and superseding it is a pure INSERT.

**Both tables are platform-level, not tenant-scoped, and that is deliberate.** The MEI
ceiling is a fact about Brazilian law, not about any one accounting firm. A per-tenant
copy would let two firms hold contradictory answers about the same statute and would
make every firm rediscover the same regulatory change independently. Both are listed
in `apps.core.rls.NON_TENANT_TABLES`; the coverage meta-test fails the build if a
table is neither tenant-scoped nor listed there.
"""

from typing import ClassVar

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantScopedModel
from apps.fiscal.mei import MEICategory
from apps.obligations.holidays import HolidayScope


class Periodicity(models.TextChoices):
    """How often an obligation recurs."""

    MONTHLY = "monthly", _("monthly")
    ANNUAL = "annual", _("annual")


class FiscalParameter(models.Model):
    """One number the engine reads, and the window in which it is the right number.

    Effective dating here is **latest-wins**, not non-overlapping ranges, and the
    distinction is load-bearing. `valid_to = NULL` means open-ended, so the 2026
    ceiling spans `[2026-01-01, ∞)` and any later open-ended row necessarily overlaps
    it. An `ExclusionConstraint` would therefore reject the exact insert that changing
    a ceiling without a deploy depends on. Overlaps are permitted; the resolver takes
    the greatest `valid_from` that is on or before the date asked about.

    The only integrity this pattern needs is that no two rows tie on
    `(key, mei_category, valid_from)` — a tie makes latest-wins depend on physical row
    order, which is not a decision anyone made.
    """

    key = models.CharField(_("key"), max_length=64)
    # Nullable on purpose, and NULL is not the same as "". NULL means the parameter
    # applies to every category (the DAS due day is the DAS due day for everyone);
    # a value means it is an override for that category alone. DJ001 warns about
    # nullable strings creating two spellings of empty, which the check constraint
    # below answers: the column admits NULL or a listed category and nothing else.
    mei_category = models.CharField(  # noqa: DJ001
        _("MEI category"),
        max_length=16,
        choices=MEICategory.choices,
        null=True,
        blank=True,
    )
    # Stored as text and cast at read. Money, percentages, and day-of-month numbers
    # all live in this one table, and a DECIMAL column would force every future
    # non-numeric parameter into a second table with a parallel resolver.
    value = models.CharField(_("value"), max_length=64)
    valid_from = models.DateField(_("valid from"))
    # Exclusive: the row covers dates strictly before valid_to. Inclusive would make
    # the changeover day ambiguous between the closing row and its successor.
    valid_to = models.DateField(_("valid to"), null=True, blank=True)
    source_note = models.TextField(_("source note"), blank=True)
    # Not blank=True. A number with no provenance cannot be audited, and this table
    # exists precisely so that "where does 81000 come from?" has an answer in the row.
    source_url = models.URLField(_("source URL"), max_length=500)

    class Meta:
        """Model metadata."""

        verbose_name = _("fiscal parameter")
        verbose_name_plural = _("fiscal parameters")
        ordering: ClassVar[list[str]] = ["key", "mei_category", "-valid_from"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # nulls_distinct=False is REQUIRED (Django 5.0+ / PostgreSQL 15+).
            # PostgreSQL treats NULLs as distinct by default, and three of the seeded
            # rows carry a NULL category — so with the default a re-run of the seed
            # would silently duplicate them and make latest-wins ambiguous exactly
            # where nobody would look.
            models.UniqueConstraint(
                fields=["key", "mei_category", "valid_from"],
                name="fiscalparameter_key_category_from_uniq",
                nulls_distinct=False,
            ),
            models.CheckConstraint(
                condition=models.Q(valid_to__isnull=True)
                | models.Q(valid_to__gt=models.F("valid_from")),
                name="fiscalparameter_validity_ordered",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            # Descending on valid_from because every read is "the newest row at or
            # before this date", which is an index-ordered first row rather than a sort.
            models.Index(
                fields=["key", "-valid_from"],
                name="fiscalparameter_key_from_idx",
            ),
        ]

    def __str__(self) -> str:
        """Identify the parameter by what it sets and when it started."""
        scope = self.mei_category or "all"
        return f"{self.key}[{scope}]={self.value} from {self.valid_from}"


class ObligationType(models.Model):
    """A recurring filing or payment, and the rule that places its deadline.

    `due_rule` is structured JSON rather than a function, because the two rules this
    product ships already disagree in direction: DAS-MEI rolls forward to the next
    business day (Resolução CGSN nº 140/2018 art. 40 §3) while DASN-SIMEI does not
    roll at all (art. 109). A third, DAE-MEI, anticipates backward. Encoding
    direction as code would mean branching on the obligation code, and the next
    obligation would add another branch. A test greps for the quoted code literal in
    behavioural source precisely so that branch cannot appear unnoticed.

    The code is the primary key rather than a surrogate: `DAS` and `DASN` are what
    every Receita Federal document already calls these, and a surrogate would add a
    join plus a second way to name the same thing.
    """

    code = models.CharField(_("code"), primary_key=True, max_length=16)
    name = models.CharField(_("name"), max_length=120)
    periodicity = models.CharField(
        _("periodicity"),
        max_length=16,
        choices=Periodicity.choices,
    )
    due_rule = models.JSONField(_("due rule"))
    source_note = models.TextField(_("source note"), blank=True)
    source_url = models.URLField(_("source URL"), max_length=500)

    class Meta:
        """Model metadata."""

        verbose_name = _("obligation type")
        verbose_name_plural = _("obligation types")
        ordering: ClassVar[list[str]] = ["code"]

    def __str__(self) -> str:
        """Identify the obligation by the code an accountant would use."""
        return self.code


class Holiday(models.Model):
    """A day on which a payment cannot be settled, and therefore a deadline moves.

    Platform-level: whether 20 November is a holiday is a fact about Brazil, not
    about any one accounting firm.

    The rows are materialized by a data migration from the computation in
    `apps.obligations.holidays`, rather than the resolver recomputing them. That is
    the difference between a calendar and a formula: a one-off day decreed by the
    government, or a municipal holiday, is an INSERT here and is invisible to code
    that recomputes from first principles.
    """

    date = models.DateField(_("date"))
    name = models.CharField(_("name"), max_length=120)
    scope = models.CharField(
        _("scope"),
        max_length=16,
        choices=HolidayScope.choices,
        default=HolidayScope.NATIONAL,
    )

    class Meta:
        """Model metadata."""

        verbose_name = _("holiday")
        verbose_name_plural = _("holidays")
        ordering: ClassVar[list[str]] = ["date", "name"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["date", "scope", "name"],
                name="holiday_date_scope_name_uniq",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            # scope leads because every lookup filters on it first: a municipal
            # holiday must not remove a business day from the national calendar.
            models.Index(fields=["scope", "date"], name="holiday_scope_date_idx"),
        ]

    def __str__(self) -> str:
        """Identify the holiday by its date and name."""
        return f"{self.date.isoformat()} {self.name}"


class SchedulerHeartbeat(models.Model):
    """Proof that the scheduler is still running, written on a timer.

    Platform-level: there is one beat process for the whole deployment, not one per
    firm, and a per-tenant heartbeat would say nothing about the process itself.

    This exists because the scheduler's failure mode is SILENCE. If the beat
    container stops, no task raises, no obligation is generated, and no error reaches
    Sentry — nothing failed, nothing happened at all. The only way to detect that is
    to require a positive signal and alarm on its absence.
    """

    name = models.CharField(_("name"), max_length=64, unique=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        """Model metadata."""

        verbose_name = _("scheduler heartbeat")
        verbose_name_plural = _("scheduler heartbeats")
        ordering: ClassVar[list[str]] = ["name"]

    def __str__(self) -> str:
        """Identify the heartbeat by who wrote it and when."""
        return f"{self.name} @ {self.updated_at.isoformat()}"


class ObligationStatus(models.TextChoices):
    """Where one obligation sits in its life."""

    SCHEDULED = "scheduled", _("scheduled")
    DUE = "due", _("due")
    PAID = "paid", _("paid")
    OVERDUE = "overdue", _("overdue")
    WAIVED = "waived", _("waived")


class Obligation(TenantScopedModel):
    """One filing or payment a client owes for one competence period.

    Both dates are stored. `nominal_due_date` is what the rule places and is stable;
    `resolved_due_date` is what the client must actually pay by and moves if the
    holiday table is later corrected. Keeping only the resolved one would make a
    calendar correction indistinguishable from a rule change after the fact.
    """

    client = models.ForeignKey(
        "clients.ClientCompany",
        on_delete=models.CASCADE,
        related_name="obligations",
        verbose_name=_("client"),
    )
    obligation_type = models.ForeignKey(
        ObligationType,
        on_delete=models.PROTECT,
        related_name="obligations",
        verbose_name=_("obligation type"),
    )
    # The first day of the competence month, which a CHECK constraint enforces so
    # that two spellings of "June 2026" cannot both exist and defeat the uniqueness
    # that makes generation idempotent.
    competence_month = models.DateField(_("competence month"))
    nominal_due_date = models.DateField(_("nominal due date"))
    resolved_due_date = models.DateField(_("resolved due date"))
    status = models.CharField(
        _("status"),
        max_length=16,
        choices=ObligationStatus.choices,
        default=ObligationStatus.SCHEDULED,
    )
    # A pointer, never a payload. The document vault is Phase 2, and a column that
    # could hold the receipt itself would quietly make this product a custodian of
    # every firm's fiscal evidence a wave before that decision is due.
    evidence_ref = models.CharField(
        _("evidence reference"),
        max_length=255,
        blank=True,
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta(TenantScopedModel.Meta):
        """Model metadata."""

        verbose_name = _("obligation")
        verbose_name_plural = _("obligations")
        ordering: ClassVar[list[str]] = ["competence_month"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # Idempotency as a DATABASE constraint, not a convention. acks_late means
            # a task that loses its worker is redelivered, so two generators can be
            # in flight at once and both find nothing when they check.
            models.UniqueConstraint(
                fields=["tenant", "client", "obligation_type", "competence_month"],
                name="obligation_tenant_client_type_competence_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(competence_month__day=1),
                name="obligation_competence_is_month_start",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            # tenant_id leads. The row-level-security predicate filters on it, and an
            # index that does not lead with it stops serving the predicate.
            models.Index(
                fields=["tenant", "resolved_due_date"],
                name="obligation_tenant_due_idx",
            ),
            models.Index(
                fields=["tenant", "status"],
                name="obligation_tenant_status_idx",
            ),
        ]

    def __str__(self) -> str:
        """Identify the obligation by what it is and which period it covers."""
        return f"{self.obligation_type_id} {self.competence_month:%Y-%m}"
