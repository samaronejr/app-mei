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

from apps.fiscal.mei import MEICategory


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
