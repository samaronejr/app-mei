"""Versioned, typed playbooks: the recurring-work templates cycles are generated from.

A `Playbook` is the durable identity — a key and a name, per firm. A
`PlaybookVersion` is an immutable snapshot of what the firm means by it at one
point in time; a `ServiceCycle` (T9) pins the version it was generated from so a
re-publish never rewrites work already in flight. `PlaybookStep` is the typed
row inside a version — typed, not a canvas: the editor is a form list and the
`kind` field is what a later generator can act on.

The invariants that keep this safe live in two places:

* the domain services (`apps.cycles.services`) own every state change and step
  edit, behind a row lock, an optimistic `version` check and a `can()` re-check;
* the rows themselves refuse what no service may do: `save()` and `delete()` on
  a non-draft version, or on a step under one, raise `ImmutableVersion`.

`position`, not `depends_on`, is the display order — dependencies are a DAG over
step keys, and reordering a list in a form must not silently rewrite the graph.
"""

from collections.abc import Iterable
from typing import ClassVar
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantScopedModel
from apps.cycles.errors import ImmutableVersion
from apps.cycles.records import (
    CycleAmendment,
    CycleEvent,
    CycleState,
    CycleTask,
    Exception,  # noqa: A004 - the plan names this domain model Exception
    ExceptionState,
    InternalNote,
    ServiceCycle,
    TaskState,
)

__all__ = [
    "CycleAmendment",
    "CycleEvent",
    "CycleState",
    "CycleTask",
    "Exception",
    "ExceptionState",
    "InternalNote",
    "OwnerRule",
    "Playbook",
    "PlaybookStep",
    "PlaybookVersion",
    "ServiceCycle",
    "StepKind",
    "TaskState",
    "VersionState",
]

KEY_MAX_LENGTH = 64
LABEL_MAX_LENGTH = 120
OWNER_RULE_MAX_LENGTH = 32
KIND_MAX_LENGTH = 32


class Playbook(TenantScopedModel):
    """The durable identity of one recurring service definition, per firm.

    `is_system` marks a playbook the platform ships as a pack (`packs.py`): the
    firm reads it and clones it, but never edits it in place — a pack the firm
    owns is no longer a pack, and a pack the platform still owns is a place
    where tenant edits would be silently overwritten on the next sync.
    """

    key = models.SlugField(
        _("key"),
        max_length=KEY_MAX_LENGTH,
    )
    name = models.CharField(_("name"), max_length=LABEL_MAX_LENGTH)
    is_system = models.BooleanField(
        _("system pack"),
        default=False,
        # editable=False would hide the field from ModelForms entirely; the flag
        # is refused at the service layer instead, where a forged POST is the
        # threat, not the admin.
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        """Model metadata."""

        verbose_name = _("playbook")
        verbose_name_plural = _("playbooks")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["tenant", "key"],
                name="playbook_tenant_key_uniq",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["tenant", "key"], name="playbook_tenant_key_idx"),
        ]

    @property
    def open_draft(self) -> "PlaybookVersion | None":
        """Return the version currently in draft, if one exists.

        Defined on the model rather than the view because the template answers
        the same question per row, and a queryset prefetched by the view makes
        this a lookup in memory, not a query per row.
        """
        return next(
            (
                version
                for version in self.versions.all()
                if version.state == VersionState.DRAFT
            ),
            None,
        )

    def __str__(self) -> str:
        """Identify the playbook by the name the firm sees."""
        return self.name


class VersionState(models.TextChoices):
    """The lifecycle of one version: drafts move, the other two never do."""

    DRAFT = "draft", _("draft")
    PUBLISHED = "published", _("published")
    RETIRED = "retired", _("retired")


class PlaybookVersion(TenantScopedModel):
    """One numbered, immutable-once-published revision of a playbook.

    `applies_to_json` is the targeting contract: `{regimes, service_kinds}` —
    the literal values T5's `TaxProfile.regime` and `Engagement.service_kind`
    enumerate, validated here against a local tuple (`apps.cycles.rules`) because
    the registry's enums land on a sibling branch and the migration of this
    field cannot depend on them.

    `version` is the optimistic-concurrency counter every state-changing service
    bumps; a caller who edited a stale snapshot loses with `VersionConflict`
    rather than overwriting the winner.
    """

    playbook = models.ForeignKey(
        Playbook,
        on_delete=models.PROTECT,
        related_name="versions",
        verbose_name=_("playbook"),
    )
    number = models.PositiveIntegerField(_("version number"))
    state = models.CharField(
        _("state"),
        max_length=16,
        choices=VersionState.choices,
        default=VersionState.DRAFT,
    )
    published_at = models.DateTimeField(
        _("published at"),
        null=True,
        blank=True,
    )
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="published_playbook_versions",
        verbose_name=_("published by"),
        null=True,
        blank=True,
    )
    applies_to_json = models.JSONField(
        _("applies to"),
        default=dict,
        blank=True,
    )
    version = models.PositiveIntegerField(_("version"), default=1)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        """Model metadata."""

        verbose_name = _("playbook version")
        verbose_name_plural = _("playbook versions")
        ordering: ClassVar[list[str]] = ["playbook", "-number"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # tenant_id leads because the composite-index guard requires the RLS
            # predicate to be servable from every multi-column index.
            models.UniqueConstraint(
                fields=["tenant", "playbook", "number"],
                name="playbookversion_playbook_number_uniq",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["tenant", "playbook"],
                name="playbookversion_tenant_pb_idx",
            ),
        ]

    def _stored_state(self) -> str | None:
        """Read the row's state as the database holds it, or None if unsaved."""
        if self._state.adding:
            return None
        # all_objects on purpose: the stored state is a fact about the row, not
        # about the ambient context — a guard that silently sees nothing without
        # a tenant in scope would let the write through exactly where it should
        # refuse. RLS still confines the read to a visible row.
        stored = (
            type(self)
            # ALL_OBJECTS_OK: pk-scoped guard read; ambient context must not decide it.
            .all_objects.filter(pk=self.pk)
            .values_list("state", flat=True)
            .first()
        )
        return str(stored) if stored is not None else None

    def save(  # type: ignore[override]
        self,
        force_insert: bool = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        """Refuse every write that would create or touch a non-draft version.

        `self.state` alone is not enough — it would let a draft row be published
        by `save(state='published')`, bypassing the DAG validation the publish
        service exists to run. The row may exist only as draft, and a stored row
        may be written only while it is still a draft. State transitions therefore
        exist only inside `apps.cycles.services`, which move the column by a
        guarded `UPDATE` rather than by `save()`.
        """
        stored = self._stored_state()
        if self.state != VersionState.DRAFT or stored not in (
            None,
            VersionState.DRAFT,
        ):
            msg = (
                "PlaybookVersion rows are written only while draft, and only "
                "through the domain services: a published or retired version is "
                "the record a generated cycle pins."
            )
            raise ImmutableVersion(msg)
        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )

    def delete(
        self, using: str | None = None, keep_parents: bool = False
    ) -> tuple[int, dict[str, int]]:
        """Refuse deletion of a version that has left draft."""
        if self._stored_state() != VersionState.DRAFT:
            msg = "Only a draft PlaybookVersion may be deleted; retire the rest."
            raise ImmutableVersion(msg)
        return super().delete(using=using, keep_parents=keep_parents)

    def __str__(self) -> str:
        """Identify the version for logs and admin rows."""
        return f"{self.playbook_id} v{self.number} ({self.state})"


class StepKind(models.TextChoices):
    """The typed step vocabulary — the full R1 list, no free-form kinds."""

    EVIDENCE_REQUEST = "evidence_request", _("evidence request")
    TASK = "task", _("task")
    REVIEW = "review", _("review")
    APPROVAL_GATE = "approval_gate", _("approval gate")
    CLIENT_PUBLICATION = "client_publication", _("client publication")
    EMAIL_NOTICE = "email_notice", _("email notice")
    CHECKPOINT = "checkpoint", _("checkpoint")


class OwnerRule(models.TextChoices):
    """Who a generated task lands with, resolved against the client at runtime."""

    ENGAGEMENT_RESPONSIBLE = "engagement_responsible", _("engagement responsible")
    PRIMARY_ASSIGNEE = "primary_assignee", _("primary assignee")
    TEAM = "team", _("the team's queue")


class PlaybookStep(TenantScopedModel):
    """One typed step inside a version, ordered by `position` for display.

    `depends_on` holds sibling step **keys** — a graph over this version only.
    Steps a tenant is told not to send yet (the pack's optional items) stay in
    the list with `is_enabled=False`: present, editable, and skipped by
    generation, rather than absent and invisible.
    """

    version = models.ForeignKey(
        PlaybookVersion,
        on_delete=models.PROTECT,
        related_name="steps",
        verbose_name=_("playbook version"),
    )
    key = models.SlugField(_("key"), max_length=KEY_MAX_LENGTH)
    position = models.PositiveIntegerField(_("position"))
    kind = models.CharField(
        _("kind"),
        max_length=KIND_MAX_LENGTH,
        choices=StepKind.choices,
    )
    label_ptbr = models.CharField(_("label"), max_length=LABEL_MAX_LENGTH)
    client_label_ptbr = models.CharField(
        _("client label"),
        max_length=LABEL_MAX_LENGTH,
        blank=True,
    )
    depends_on = models.JSONField(
        _("depends on"),
        default=list,
        blank=True,
    )
    owner_rule = models.CharField(
        _("owner rule"),
        max_length=OWNER_RULE_MAX_LENGTH,
        choices=OwnerRule.choices,
        default=OwnerRule.TEAM,
    )
    due_rule_json = models.JSONField(
        _("due rule"),
        default=dict,
        blank=True,
    )
    evidence_required = models.BooleanField(_("evidence required"), default=False)
    client_visible = models.BooleanField(_("client visible"), default=False)
    is_enabled = models.BooleanField(_("enabled"), default=True)

    class Meta(TenantScopedModel.Meta):
        """Model metadata."""

        verbose_name = _("playbook step")
        verbose_name_plural = _("playbook steps")
        ordering: ClassVar[list[str]] = ["version", "position"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["tenant", "version", "key"],
                name="playbookstep_version_key_uniq",
            ),
            models.UniqueConstraint(
                fields=["tenant", "version", "position"],
                name="playbookstep_version_position_uniq",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["tenant", "version"],
                name="playbookstep_tenant_ver_idx",
            ),
        ]

    def _version_state(self, version_id: UUID | None) -> str | None:
        """Read a version's state as the database holds it.

        `self.version.state` would answer from whatever instance was fetched
        with the step — the point of the guard is what is stored, not what a
        caller last loaded.
        """
        if version_id is None:
            return None
        stored = (
            # ALL_OBJECTS_OK: pk-scoped guard read — the same reason
            # PlaybookVersion._stored_state uses the unscoped manager.
            PlaybookVersion.all_objects.filter(pk=version_id)
            .values_list("state", flat=True)
            .first()
        )
        return str(stored) if stored is not None else None

    def _stored_version_state(self) -> str | None:
        """Return the state of the parent this in-memory object names."""
        return self._version_state(self.version_id)

    def _persisted_parent_state(self) -> str | None:
        """Return the state of the parent the STORED row names — different thing.

        On an update, `self.version_id` is where the step is being written TO;
        the row already in the table names where it comes FROM. A published
        version's membership cannot shrink because a caller repointed the FK —
        the source parent's state is checked before the target's.
        """
        if self.pk is None:
            return None
        stored_parent = (
            # ALL_OBJECTS_OK: pk-scoped guard read — ambient context must not
            # decide whether a write is legal (see _stored_state).
            PlaybookStep.all_objects.filter(pk=self.pk)
            .values_list("version_id", flat=True)
            .first()
        )
        return self._version_state(stored_parent)

    def save(  # type: ignore[override]
        self,
        force_insert: bool = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        """Refuse writes under a non-draft version, whatever the field is.

        Both ends of a reparent are guarded: the version this step is stored
        under cannot lose it, and a non-draft version cannot gain it. A draft
        step moved into another DRAFT version is still legal — drafts are
        mutable by definition.
        """
        persisted = self._persisted_parent_state()
        if persisted is not None and persisted != VersionState.DRAFT:
            msg = (
                "A step belonging to a published or retired version cannot be "
                "rewritten, reparented or removed — clone or publish a new "
                "version instead."
            )
            raise ImmutableVersion(msg)
        if self._stored_version_state() != VersionState.DRAFT:
            msg = (
                "Steps of a published or retired version are immutable; clone or "
                "publish a new version to change them."
            )
            raise ImmutableVersion(msg)
        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )

    def delete(
        self, using: str | None = None, keep_parents: bool = False
    ) -> tuple[int, dict[str, int]]:
        """Refuse deletion under a non-draft version — the stored parent decides."""
        persisted = self._persisted_parent_state()
        if persisted is not None and persisted != VersionState.DRAFT:
            msg = (
                "A step belonging to a published or retired version cannot be deleted."
            )
            raise ImmutableVersion(msg)
        if self._stored_version_state() != VersionState.DRAFT:
            msg = "Steps of a published or retired version cannot be deleted."
            raise ImmutableVersion(msg)
        return super().delete(using=using, keep_parents=keep_parents)

    def clean(self) -> None:
        """Keep `depends_on` a list of keys rather than arbitrary JSON."""
        super().clean()
        if not isinstance(self.depends_on, list) or not all(
            isinstance(key, str) for key in self.depends_on
        ):
            msg = _("depends_on must be a list of step keys.")
            raise ValidationError({"depends_on": msg})

    def __str__(self) -> str:
        """Identify the step within its version."""
        return f"{self.version_id}:{self.key}"
