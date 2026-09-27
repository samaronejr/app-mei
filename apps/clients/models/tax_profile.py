"""The client's tax regime, effective-dated, corrected never overwritten.

Two clocks are kept on every row, and the distinction is the whole point:

* **the VALID clock** (`valid_from` / `valid_to`) says when the claim is true in the
  client's life — the regime they were under in March is a different question from
  the regime they are under today;
* **the RECORDED clock** (`recorded_at` / `superseded_at`) says when THIS SYSTEM knew
  the claim — a backfill that lands today about a client opened in 2021 is a fact
  about today's knowledge, not about 2021.

Non-overlap is enforced only among rows that are still *believed*
(`superseded_at IS NULL`), because a correction is allowed to disagree with what was
recorded before and is required to leave it readable. The `btree_gist` exclusion
constraint in the migration is that rule made structural: two current rows for one
client can never cover the same day, so `profile_at` never has to arbitrate a tie,
and a gap in the timeline reads as *unknown* rather than as a guess.

Corrections therefore never UPDATE a row's claim. `correct_tax_profile` locks the
client's timeline, stamps `superseded_at` on every current row the correction
overlaps, re-asserts whatever slices of those rows lie outside the corrected range,
and inserts the new claim — atomically, so a crash mid-correction cannot leave the
timeline halved.
"""

from typing import ClassVar

from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields.ranges import RangeOperators
from django.db import models
from django.db.models import F, Func, Q, Value
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantScopedModel
from apps.fiscal.mei import MEICategory


class TaxRegime(models.TextChoices):
    """The regimes a Brazilian client's firm may need to name.

    `UNKNOWN` is a real value, not the absence of one: the backfill writes it for
    every client the legacy flag did not mark as MEI, because "the old schema could
    not say" is a different answer from "not applicable" and must not read as one.
    """

    MEI = "mei", _("MEI")
    SIMPLES = "simples", _("Simples Nacional")
    PRESUMIDO = "presumido", _("Lucro Presumido")
    REAL = "real", _("Lucro Real")
    ISENTO = "isento", _("exempt")
    UNKNOWN = "unknown", _("unknown")


class ProfileConfidence(models.TextChoices):
    """How much weight a profile claim carries.

    `ASSERTED_BY_LEGACY_FLAG` marks rows the backfill derived from the old boolean:
    it means "this is what `is_mei` said", no more and no less, and it is what lets a
    review tell a migrated guess from a person's decision.
    """

    ASSERTED_BY_LEGACY_FLAG = "asserted_by_legacy_flag", _("asserted by legacy flag")
    STAFF_RECORDED = "staff_recorded", _("recorded by staff")


class TaxProfile(TenantScopedModel):
    """One assertion about the client's tax regime over one validity range."""

    client = models.ForeignKey(
        "clients.ClientCompany",
        on_delete=models.CASCADE,
        related_name="tax_profiles",
        verbose_name=_("client"),
    )
    regime = models.CharField(
        _("regime"),
        max_length=16,
        choices=TaxRegime.choices,
        default=TaxRegime.UNKNOWN,
    )
    # Meaningful only while regime is MEI; kept nullable because an unknown regime
    # has no category to assert and a blank string would be a second spelling of
    # "none". The CHECK below keeps the two columns consistent.
    mei_category = models.CharField(  # noqa: DJ001
        _("MEI category"),
        max_length=16,
        choices=MEICategory.choices,
        null=True,
        blank=True,
    )
    valid_from = models.DateField(_("valid from"))
    # NULL is open-ended: the claim holds until something supersedes it. Distinct
    # from a far-future date, which would quietly invent an expiry nobody set.
    valid_to = models.DateField(_("valid to"), null=True, blank=True)
    provenance = models.CharField(
        _("provenance"),
        max_length=32,
        help_text=_("how this row was created (e.g. backfill_v1, staff entry)"),
    )
    confidence = models.CharField(
        _("confidence"),
        max_length=32,
        choices=ProfileConfidence.choices,
        default=ProfileConfidence.STAFF_RECORDED,
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="recorded_tax_profiles",
        verbose_name=_("recorded by"),
        null=True,
        blank=True,
    )
    # When THIS SYSTEM recorded the claim — always now() for corrections, and the
    # insertion instant for the backfill, because the recorded clock must not be
    # backdated into the past the valid clock describes.
    recorded_at = models.DateTimeField(_("recorded at"), default=timezone.now)
    # NULL while the row is believed. Once stamped, the row leaves the "current"
    # set that the exclusion constraint polices and that profile_at reads.
    superseded_at = models.DateTimeField(_("superseded at"), null=True, blank=True)
    # The superseded row this assertion replaces or restates. On a correction it
    # names the claim it overruled; on a split fragment it names the row it re-asserts
    # unchanged. The client-paired composite FK makes a cross-client link
    # unrepresentable at the database layer.
    correction_of = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        related_name="corrections",
        verbose_name=_("correction of"),
        null=True,
        blank=True,
    )
    note = models.TextField(_("note"), blank=True)

    class Meta(TenantScopedModel.Meta):
        """Model metadata."""

        verbose_name = _("tax profile")
        verbose_name_plural = _("tax profiles")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # The referent the client-paired `correction_of` composite FK points at.
            models.UniqueConstraint(
                fields=["tenant", "client", "id"],
                name="taxprofile_tenant_client_id_uniq",
            ),
            models.CheckConstraint(
                condition=Q(valid_to__isnull=True) | Q(valid_to__gt=F("valid_from")),
                name="taxprofile_valid_range_nonempty",
            ),
            # Only one current belief per client per day. btree_gist supplies the
            # equality operator classes uuid keys need inside a GiST index; the
            # migration asserts the extension exists rather than letting the failure
            # surface as an operator-class error. The WHERE clause is the recorded
            # clock: superseded rows keep their history instead of competing with
            # whatever replaced them.
            ExclusionConstraint(
                name="taxprofile_current_range_excl",
                expressions=[
                    (F("tenant_id"), RangeOperators.EQUAL),
                    (F("client_id"), RangeOperators.EQUAL),
                    (
                        Func(
                            F("valid_from"),
                            F("valid_to"),
                            Value("[)"),
                            function="daterange",
                        ),
                        RangeOperators.OVERLAPS,
                    ),
                ],
                condition=Q(superseded_at__isnull=True),
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["tenant", "client"], name="taxprofile_tenant_client_idx"
            ),
        ]

    def __str__(self) -> str:
        """Identify the claim by regime and validity."""
        return f"{self.client_id} / {self.regime} from {self.valid_from}"
