"""Immutable assertions about what a source observed, per subject and period.

An observation is a *fact about a fact*: "the legacy status column said this
obligation was paid" is an assertion with a source, a time and a confidence, and it
is a different object from the obligation it describes. Keeping them separate is
S6.4's status separation (invariant 8): `Obligation.status` keeps meaning work
state, and whether the money moved is answered here, by a row that says WHERE the
claim came from rather than by a column that cannot.

Two properties are load-bearing, and both are structural rather than conventional:

* **Append-only.** The migration runs `make_append_only`, so the table refuses
  UPDATE and DELETE for every role including its owner. A correction is a NEW row
  pointing back through `supersedes`; the overruled claim stays readable because
  a payment trail that can be rewritten is not a trail.
* **Provenance is immutable.** `source_kind` names who made the claim — a legacy
  column, a person, an official channel — and invariant 10 ("manual stays manual")
  is the rule that a `staff_recorded` row may never be *promoted* into
  `official_api`. A later verification writes a new row; it does not upgrade the
  old one.

`subject_ref` is a real foreign key to `Obligation`, paired on
`(tenant_id, client_id)` the way every client-scoped reference in this schema is:
referential checks bypass row-level security, so without the pairing a portal or
staff write could pin an observation onto another client's obligation and read
existence out of the violations. In R1 `subject_kind` has exactly one value for
that reason — the composite key is the mechanism, and other subject kinds arrive
with the tables that carry them.
"""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantScopedModel


class ObservationSubjectKind(models.TextChoices):
    """What an observation is about. One value in R1, on purpose."""

    OBLIGATION = "obligation", _("obligation")


class ObservationKind(models.TextChoices):
    """Which fact about the subject this row asserts.

    `CALENDAR_SCHEDULED` (T17) asserts only that the obligation's stored schedule
    exists -- its nominal and resolved dates for one competence -- never that a
    guide was issued, anything was filed, or money moved. Naming the calendar
    fact `guide_issued` would dress a rule output as an issuance event.
    """

    PAYMENT = "payment", _("payment")
    FILING = "filing", _("filing")
    GUIDE_ISSUED = "guide_issued", _("guide issued")
    CALENDAR_SCHEDULED = "calendar_scheduled", _("calendar scheduled")


class ObservationSourceKind(models.TextChoices):
    """Who asserted the fact — the column a reader weighs the claim by.

    `LEGACY_STATUS` marks rows the data migration derived from
    `Obligation.status`, so a `paid` the old enum carried is labelled as what it
    is — a manual registration — rather than dressed as a verification.
    """

    OFFICIAL_API = "official_api", _("official API")
    OFFICIAL_FILE_IMPORT = "official_file_import", _("official file import")
    CLIENT_SUBMITTED = "client_submitted", _("client submitted")
    STAFF_RECORDED = "staff_recorded", _("staff recorded")
    DETERMINISTIC_RULE = "deterministic_rule", _("deterministic rule")
    AI_INFERENCE = "ai_inference", _("AI inference")
    LEGACY_STATUS = "legacy_status", _("legacy status column")


class ObservationVerificationStatus(models.TextChoices):
    """How far the claim has been checked, independent of who made it.

    `UNKNOWN` is the default and the honest floor: a row that never reached a
    verification channel is unknown, not pending — pending implies somebody is
    checking, and nobody is.
    """

    VERIFIED = "verified", _("verified")
    PENDING_REVIEW = "pending_review", _("pending review")
    UNKNOWN = "unknown", _("unknown")
    UNSUPPORTED = "unsupported", _("unsupported")
    STALE = "stale", _("stale")
    REFRESH_FAILED = "refresh_failed", _("refresh failed")
    UNAUTHORIZED = "unauthorized", _("unauthorized")
    NOT_APPLICABLE = "not_applicable", _("not applicable")


class ObservationOutcome(models.TextChoices):
    """What the assertion ended up meaning for the claim it makes (T17).

    `NONE` marks rows that never declared an outcome — the T9 backfill and any
    row written before the vocabulary existed. `SUBMITTED` is a client file
    awaiting the firm; `ACCEPTED`/`REJECTED` are its decision; `RECORDED`,
    `CORRECTED` and `RETRACTED` are the staff-fact lifecycle; `SCHEDULED` is
    the calendar producer's; `REFRESH_FAILED` is an official refresh that
    failed and therefore proves nothing new.
    """

    NONE = "none", _("no outcome recorded")
    SUBMITTED = "submitted", _("submitted")
    ACCEPTED = "accepted", _("accepted")
    REJECTED = "rejected", _("rejected")
    RECORDED = "recorded", _("recorded")
    CORRECTED = "corrected", _("corrected")
    RETRACTED = "retracted", _("retracted")
    SCHEDULED = "scheduled", _("scheduled")
    REFRESH_FAILED = "refresh_failed", _("refresh failed")


class SourceObservation(TenantScopedModel):
    """One sourced, never-rewritten assertion about one subject in one period."""

    client = models.ForeignKey(
        "clients.ClientCompany",
        on_delete=models.CASCADE,
        related_name="source_observations",
        verbose_name=_("client"),
    )
    subject_kind = models.CharField(
        _("subject kind"),
        max_length=32,
        choices=ObservationSubjectKind.choices,
        default=ObservationSubjectKind.OBLIGATION,
    )
    # The obligation this row describes. A real foreign key rather than a bare
    # UUID: the client-paired composite constraint in the migration is what makes
    # a cross-client subject unrepresentable rather than merely invisible.
    subject_ref = models.ForeignKey(
        "obligations.Obligation",
        on_delete=models.PROTECT,
        related_name="observations",
        verbose_name=_("subject"),
    )
    kind = models.CharField(
        _("kind"),
        max_length=32,
        choices=ObservationKind.choices,
    )
    # The competence the assertion covers; nullable because later subject kinds are
    # not all monthly. For obligation facts it is the obligation's own competence
    # month — the period the payment answers, not the day it was noticed.
    period = models.DateField(_("period"), null=True, blank=True)
    source_kind = models.CharField(
        _("source kind"),
        max_length=32,
        choices=ObservationSourceKind.choices,
    )
    # When the SOURCE claims the fact happened. For the legacy-status backfill this
    # is the obligation row's last write — the honest timestamp the old schema
    # carried — never the migration's own clock, which would claim the payment was
    # observed on the day the migration ran.
    observed_at = models.DateTimeField(_("observed at"))
    value_json = models.JSONField(
        _("value"),
        default=dict,
        blank=True,
        help_text=_("the asserted payload; identifiers and codes only, never text"),
    )
    verification_status = models.CharField(
        _("verification status"),
        max_length=32,
        choices=ObservationVerificationStatus.choices,
        default=ObservationVerificationStatus.UNKNOWN,
    )
    # The observation this one replaces. NULL on the first assertion about a
    # subject; corrections supersede rather than update, and the paired composite
    # FK makes a cross-client chain unrepresentable.
    supersedes = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        related_name="successors",
        verbose_name=_("supersedes"),
        null=True,
        blank=True,
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="recorded_observations",
        verbose_name=_("recorded by"),
        null=True,
        blank=True,
    )
    # What this assertion ended up meaning (T17): a rejected submission and a
    # retracted staff note are different outcomes of the same unsupported
    # status, and only the outcome can say which.
    outcome = models.CharField(
        _("outcome"),
        max_length=32,
        choices=ObservationOutcome.choices,
        default=ObservationOutcome.NONE,
    )
    # The reference the staffer named for a manual fact — a guia number, a
    # protocol, a receipt identifier. Short because it is a pointer, never
    # prose: reasons belong to the review that produced them.
    source_ref = models.CharField(_("source reference"), max_length=255, blank=True)
    # The submitted file this assertion cites as evidence (T17). Client-paired:
    # the composite constraint in the migration makes a cross-client link
    # unrepresentable. Optional because staff-recorded facts may name none; a
    # receipt assertion (submission, acceptance, rejection) may not omit it.
    evidence_link = models.ForeignKey(
        "cycles.EvidenceLink",
        on_delete=models.PROTECT,
        related_name="observations",
        verbose_name=_("evidence link"),
        null=True,
        blank=True,
    )
    # When THIS SYSTEM wrote the row — distinct from `observed_at`, which is the
    # source's clock. A backfill observed months ago is still recorded now.
    recorded_at = models.DateTimeField(_("recorded at"), auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        """Model metadata."""

        verbose_name = _("source observation")
        verbose_name_plural = _("source observations")
        ordering: ClassVar[list[str]] = ["observed_at", "pk"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # The referent this table's own client-paired `supersedes` composite FK
            # points at — the subject link borrows obligations_obligation's existing
            # (tenant, client, id) unique instead.
            models.UniqueConstraint(
                fields=["tenant", "client", "id"],
                name="sourceobservation_tenant_client_id_uniq",
            ),
            # T17: one observation chain may cite a link at most once -- the
            # submission it was created from. A second assertion about the same
            # link (the review's outcome) supersede-chains off it rather than
            # duplicating the submission identity.
            models.UniqueConstraint(
                fields=["tenant", "client", "evidence_link"],
                condition=models.Q(evidence_link__isnull=False),
                name="sourceobservation_evidence_link_uniq",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            # tenant_id leads so the row-level-security predicate stays
            # index-served; client+subject is the read the portal page makes.
            models.Index(
                fields=["tenant", "client", "subject_ref"],
                name="obs_tenant_client_subject_idx",
            ),
        ]

    def __str__(self) -> str:
        """Identify the assertion by subject, kind and source."""
        return f"{self.subject_ref_id} / {self.kind} via {self.source_kind}"
