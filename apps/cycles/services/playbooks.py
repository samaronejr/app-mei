"""The playbook lifecycle services — see the package docstring for the contract.

State changes never flow through `PlaybookVersion.save()`: the model refuses
them on purpose, so this module is the single place an UPDATE may move `state`,
and each move here still passes through validation, the lock and the audit row.
"""

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Final
from uuid import UUID

from django.apps import apps as django_apps
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditAction
from apps.audit.services import ObjectRef, record_event
from apps.authz.services import can
from apps.core.tenancy import tenant_context
from apps.cycles.capabilities import PLAYBOOK_GATE
from apps.cycles.errors import (
    IllegalTransition,
    ImmutableVersion,
    PlaybookValidationError,
    VersionConflict,
)
from apps.cycles.models import (
    Playbook,
    PlaybookStep,
    PlaybookVersion,
    VersionState,
)
from apps.cycles.rules import (
    StepSpec,
    next_period_start,
    validate_draft,
)
from apps.cycles.targeting import matching_clients
from apps.obligations.models import ObligationType

if TYPE_CHECKING:
    from apps.tenants.models import Tenant

# The states in which a generated cycle is still in flight. `cycles_servicecycle`
# lands with T9; the set is pinned here so the preview's "pinned to older
# versions" count reads one definition the generator will share.
OPEN_CYCLE_STATES: Final[frozenset[str]] = frozenset(
    {
        "planejado",
        "em_andamento",
        "aguardando_cliente",
        "em_revisao",
        "aguardando_aprovacao",
        "em_execucao",
        "bloqueado",
    }
)


@dataclass(frozen=True, slots=True)
class PublishPreview:
    """What a publish would generate: future cycles and what stays pinned.

    `client_ids` is capped for display — the count is authoritative, the sample
    exists so the editor can name real clients rather than a bare number.
    """

    period_start: date
    matching_clients: int
    client_ids: tuple[UUID, ...]
    pinned_open_cycles: int


def _denied() -> PermissionDenied:
    return PermissionDenied("This account may not manage playbooks.")


def _require_actor(actor: User | None, *, tenant: "Tenant", system_row: bool) -> None:
    """Apply the authorization contract inside the lock — never at the edge only.

    `actor=None` is the platform installing packs: it may touch ONLY system
    rows. A signed-in account needs the playbook capability AND must never edit
    a system row in place — pack content is owned by the platform, so a tenant
    edit would either be overwritten by the next sync or silently diverge the
    pack across firms.
    """
    if actor is None:
        if not system_row:
            msg = "System writers may only touch system playbooks."
            raise ImmutableVersion(msg)
        return
    if not can(actor, PLAYBOOK_GATE, tenant_id=tenant.pk):
        raise _denied()
    if system_row:
        msg = (
            "Pacotes do sistema não podem ser editados; clone o playbook para "
            "personalizá-lo."
        )
        raise PermissionDenied(msg)


def _lock_playbook(tenant: "Tenant", playbook_pk: UUID) -> Playbook:
    """Take the aggregate lock, or 404-shaped refusal: the row is not there.

    `all_objects` keyed on `(tenant, pk)` rather than the scoped manager: the
    lookup is the authorization boundary — a pk from another tenant must answer
    "not found", and routing it through `objects` would let a missing tenant
    context read as a missing row. Under RLS the predicate repeats the same
    check, so the two layers agree.
    """
    playbook = (
        # ALL_OBJECTS_OK: the tenant predicate is in the filter — the lookup IS
        # the authorization boundary, as the docstring states.
        Playbook.all_objects.filter(tenant_id=tenant.pk, pk=playbook_pk)
        .select_for_update()
        .first()
    )
    if playbook is None:
        msg = f"No playbook {playbook_pk} exists for this firm."
        raise Playbook.DoesNotExist(msg)
    return playbook


def _locked_version(
    playbook: Playbook,
    number: int,
    expected_version: int,
) -> PlaybookVersion:
    """Read the version under the held lock and check the optimistic counter.

    The counter is on the version, not the playbook: what races is two editors
    of the same draft, and a shared playbook counter would force a pointless
    409 between editors of different versions.
    """
    version = playbook.versions.filter(number=number).first()
    if version is None:
        msg = f"Version {number} does not exist on {playbook.key}."
        raise PlaybookVersion.DoesNotExist(msg)
    if version.version != expected_version:
        msg = (
            f"Version {version.number} is now at revision {version.version}; "
            f"the edit carried {expected_version}. Reload before writing."
        )
        raise VersionConflict(msg)
    return version


def _replace_steps(version: PlaybookVersion, specs: list[StepSpec]) -> None:
    """Rewrite a draft's step list wholesale under the held lock.

    Delete-and-recreate rather than in-place diffing: `position` is the display
    order and a step's identity is its key, so a partial update would have to
    re-derive what a clean rewrite states directly. Drafts only — the caller has
    already proven the state under the lock.
    """
    version.steps.all().delete()
    PlaybookStep.objects.bulk_create(
        [
            PlaybookStep(
                tenant_id=version.tenant_id,
                version=version,
                key=spec.key,
                position=index,
                kind=spec.kind,
                label_ptbr=spec.label_ptbr,
                client_label_ptbr=spec.client_label_ptbr,
                depends_on=list(spec.depends_on),
                owner_rule=spec.owner_rule,
                due_rule_json=spec.due_rule,
                evidence_required=spec.evidence_required,
                client_visible=spec.client_visible,
                is_enabled=spec.is_enabled,
            )
            for index, spec in enumerate(specs)
        ]
    )


def _known_obligations() -> set[str]:
    """List the obligation-type codes a due rule may name.

    `ObligationType` is platform reference data (not tenant-scoped), so the read
    needs no context and uses the plain manager.
    """
    return set(ObligationType.objects.values_list("code", flat=True))


def create_playbook(  # noqa: PLR0913 — the signature is the contract
    *,
    actor: User | None,
    tenant: "Tenant",
    key: str,
    name: str,
    applies_to: object | None = None,
    steps: list[StepSpec] | None = None,
) -> Playbook:
    """Create a tenant playbook with one draft version, atomically.

    `actor=None` is refused: the platform's own playbooks come from
    `install_system_packs`, which owns the `is_system` flag a caller must never
    supply through here.
    """
    if actor is None:
        msg = "Tenant playbooks are created by accounts, not the installer."
        raise PermissionDenied(msg)
    with tenant_context(tenant.pk):
        _require_actor(actor, tenant=tenant, system_row=False)
        playbook = Playbook.objects.create(
            tenant=tenant,
            key=key,
            name=name,
            is_system=False,
        )
        draft = PlaybookVersion(
            tenant=tenant,
            playbook=playbook,
            number=1,
            state=VersionState.DRAFT,
            applies_to_json=validate_draft(
                applies_to=applies_to or {},
                steps=steps or [],
                known_obligations=_known_obligations(),
                for_publish=False,
            ),
        )
        draft.save()
        if steps:
            _replace_steps(draft, steps)
        record_event(
            action=AuditAction.PLAYBOOK_UPDATED,
            tenant_id=tenant.pk,
            actor=actor,
            obj=ObjectRef(type="Playbook", id=str(playbook.pk)),
            metadata={"created": True, "key": playbook.key},
        )
    return playbook


def clone_playbook(
    *,
    actor: User,
    tenant: "Tenant",
    playbook_pk: UUID,
) -> Playbook:
    """Copy a playbook — system packs included — into an editable tenant row.

    This is the supported way to customise a system pack: the clone is an
    ordinary playbook whose first draft copies the pack's latest published
    steps. The origin stays `is_system` and immutable.
    """
    with tenant_context(tenant.pk):
        source = _lock_playbook(tenant, playbook_pk)
        _require_actor(actor, tenant=tenant, system_row=False)
        base = (
            source.versions.filter(state=VersionState.PUBLISHED)
            .order_by("-number")
            .first()
            or source.versions.order_by("-number").first()
        )
        key = _free_clone_key(tenant, source.key)
        clone = Playbook.objects.create(
            tenant=tenant,
            key=key,
            name=f"{source.name} (cópia)"[:120],
            is_system=False,
        )
        draft = PlaybookVersion(
            tenant=tenant,
            playbook=clone,
            number=1,
            state=VersionState.DRAFT,
            applies_to_json=base.applies_to_json if base else {},
        )
        draft.save()
        if base is not None:
            _replace_steps(draft, [_spec_of(step) for step in base.steps.all()])
        record_event(
            action=AuditAction.PLAYBOOK_UPDATED,
            tenant_id=tenant.pk,
            actor=actor,
            obj=ObjectRef(type="Playbook", id=str(clone.pk)),
            metadata={"cloned_from": str(source.pk), "key": clone.key},
        )
    return clone


def _free_clone_key(tenant: "Tenant", base_key: str) -> str:
    """Find `key`, `key-2`, `key-3`... unused within the tenant."""
    existing = set(
        # ALL_OBJECTS_OK: tenant-keyed key-space probe inside the held lock.
        Playbook.all_objects.filter(
            tenant_id=tenant.pk, key__startswith=base_key
        ).values_list("key", flat=True)
    )
    if base_key not in existing:
        return base_key
    counter = 2
    while f"{base_key}-{counter}" in existing:
        counter += 1
    return f"{base_key}-{counter}"


def _spec_of(step: PlaybookStep) -> StepSpec:
    """Round-trip a stored step back into the editable spec."""
    return StepSpec(
        key=step.key,
        kind=step.kind,
        label_ptbr=step.label_ptbr,
        client_label_ptbr=step.client_label_ptbr,
        depends_on=tuple(str(k) for k in step.depends_on),
        owner_rule=step.owner_rule,
        due_rule=dict(step.due_rule_json),
        evidence_required=step.evidence_required,
        client_visible=step.client_visible,
        is_enabled=step.is_enabled,
    )


def create_draft_version(
    *,
    actor: User | None,
    tenant: "Tenant",
    playbook_pk: UUID,
) -> PlaybookVersion:
    """Open the next numbered draft, seeded from the latest published version.

    A copy, not a reference: steps land as new rows so the published version the
    draft started from stays byte-identical while the draft is edited.
    """
    with tenant_context(tenant.pk):
        playbook = _lock_playbook(tenant, playbook_pk)
        _require_actor(actor, tenant=tenant, system_row=playbook.is_system)
        latest = playbook.versions.order_by("-number").first()
        if latest is not None and latest.state == VersionState.DRAFT:
            msg = "A draft version already exists; edit it instead."
            raise IllegalTransition(msg)
        base = (
            playbook.versions.filter(state=VersionState.PUBLISHED)
            .order_by("-number")
            .first()
        )
        draft = PlaybookVersion(
            tenant=tenant,
            playbook=playbook,
            number=(latest.number + 1) if latest else 1,
            state=VersionState.DRAFT,
            applies_to_json=base.applies_to_json if base else {},
        )
        draft.save()
        if base is not None:
            _replace_steps(draft, [_spec_of(step) for step in base.steps.all()])
        record_event(
            action=AuditAction.PLAYBOOK_UPDATED,
            tenant_id=tenant.pk,
            actor=actor,
            obj=ObjectRef(type="PlaybookVersion", id=str(draft.pk)),
            metadata={
                "playbook": str(playbook.pk),
                "number": draft.number,
                "created": True,
            },
        )
    return draft


def update_draft(  # noqa: PLR0913 — one parameter per editable field
    *,
    actor: User | None,
    tenant: "Tenant",
    playbook_pk: UUID,
    number: int,
    expected_version: int,
    steps: list[StepSpec],
    applies_to: object | None = None,
    name: str | None = None,
) -> PlaybookVersion:
    """Replace a draft's content, validated as a whole before anything writes.

    The version counter bumps on every accepted write so a stale `move`/`save`
    issued from a page loaded before this one lands as `VersionConflict`, not as
    silently lost work.
    """
    with tenant_context(tenant.pk):
        playbook = _lock_playbook(tenant, playbook_pk)
        _require_actor(actor, tenant=tenant, system_row=playbook.is_system)
        version = _locked_version(playbook, number, expected_version)
        if version.state != VersionState.DRAFT:
            # ImmutableVersion, not IllegalTransition: a content write to a
            # non-draft is not a transition at all — it is the write the row's
            # own save() guard exists to refuse.
            msg = "Only a draft version may be edited."
            raise ImmutableVersion(msg)
        normalized = validate_draft(
            applies_to=(
                applies_to if applies_to is not None else version.applies_to_json
            ),
            steps=steps,
            known_obligations=_known_obligations(),
            for_publish=False,
        )
        if name is not None and name.strip():
            playbook.name = name.strip()
            playbook.save(update_fields=["name"])
        _replace_steps(version, steps)
        # ALL_OBJECTS_OK: pk-scoped update; save() would refuse the state
        # column anyway, which is why this is an UPDATE and not a save.
        PlaybookVersion.all_objects.filter(pk=version.pk).update(
            applies_to_json=normalized,
            version=version.version + 1,
        )
        version.refresh_from_db()
        record_event(
            action=AuditAction.PLAYBOOK_UPDATED,
            tenant_id=tenant.pk,
            actor=actor,
            obj=ObjectRef(type="PlaybookVersion", id=str(version.pk)),
            metadata={
                "playbook": str(playbook.pk),
                "number": version.number,
                "steps": len(steps),
            },
        )
    return version


def move_step(  # noqa: PLR0913 — move needs its own coordinates
    *,
    actor: User | None,
    tenant: "Tenant",
    playbook_pk: UUID,
    number: int,
    expected_version: int,
    step_key: str,
    direction: str,
) -> PlaybookVersion:
    """Move one step one position up or down — the keyboard reorder path.

    A dedicated operation rather than "save the whole list" so the button's
    POST carries exactly the intent it performs, and so two editors' moves are
    ordered by the lock and the version check like every other write.
    """
    with tenant_context(tenant.pk):
        playbook = _lock_playbook(tenant, playbook_pk)
        _require_actor(actor, tenant=tenant, system_row=playbook.is_system)
        version = _locked_version(playbook, number, expected_version)
        if version.state != VersionState.DRAFT:
            msg = "Only a draft version may be reordered."
            raise ImmutableVersion(msg)
        ordered = list(version.steps.order_by("position"))
        index = next(
            (i for i, step in enumerate(ordered) if step.key == step_key),
            None,
        )
        if index is None:
            msg = f"No step {step_key!r} exists on this version."
            raise PlaybookValidationError([msg])
        target = index - 1 if direction == "up" else index + 1
        if direction not in {"up", "down"} or not 0 <= target < len(ordered):
            msg = "The step cannot move further in that direction."
            raise PlaybookValidationError([msg])
        ordered[index], ordered[target] = ordered[target], ordered[index]
        _replace_steps(version, [_spec_of(step) for step in ordered])
        # ALL_OBJECTS_OK: pk-scoped counter bump; see update_draft.
        PlaybookVersion.all_objects.filter(pk=version.pk).update(
            version=version.version + 1,
        )
        version.refresh_from_db()
        record_event(
            action=AuditAction.PLAYBOOK_UPDATED,
            tenant_id=tenant.pk,
            actor=actor,
            obj=ObjectRef(type="PlaybookVersion", id=str(version.pk)),
            metadata={
                "playbook": str(playbook.pk),
                "number": version.number,
                "moved": step_key,
                "direction": direction,
            },
        )
    return version


def _specs_of_version(version: PlaybookVersion) -> list[StepSpec]:
    """Read a version's steps back as specs for validation."""
    return [_spec_of(step) for step in version.steps.order_by("position")]


def _compute_preview(
    *,
    tenant: "Tenant",
    playbook: Playbook,
    version: PlaybookVersion,
    normalized: dict[str, list[str]],
    on: date | None,
) -> PublishPreview:
    """Count what a publish of this draft would generate — read-only.

    `period_start` is the next calendar month — generation (T9) creates cycles
    per period. `pinned_open_cycles` counts open cycles still pinned to older
    versions of the same playbook; `ServiceCycle` does not exist on this
    branch's base, so the lookup degrades to 0 until T9 lands the model this
    signature is already shaped for.
    """
    period_start = next_period_start(on or timezone.localdate())
    matched = matching_clients(tenant, normalized, period_start)
    try:
        cycle_model = django_apps.get_model("cycles", "ServiceCycle")
    except LookupError:
        cycle_model = None
    pinned = 0
    if cycle_model is not None:
        # ALL_OBJECTS_OK: tenant-keyed count; T9's table carries the same
        # tenant predicate this filter names.
        pinned = cycle_model.all_objects.filter(
            tenant_id=tenant.pk,
            playbook_version__playbook_id=playbook.pk,
            playbook_version__number__lt=version.number,
            state__in=OPEN_CYCLE_STATES,
        ).count()
    return PublishPreview(
        period_start=period_start,
        matching_clients=matched.count(),
        client_ids=tuple(matched.values_list("pk", flat=True)[:20]),
        pinned_open_cycles=pinned,
    )


def preview_publish(
    *,
    actor: User | None,
    tenant: "Tenant",
    playbook_pk: UUID,
    number: int,
    on: date | None = None,
) -> PublishPreview:
    """Validate a draft and report what publishing it would generate.

    Read-only by contract — S8.1 requires publishing to be preceded by this
    preview, so it runs the same `validate_draft` the publish does and refuses
    with the same errors, but never writes.
    """
    with tenant_context(tenant.pk):
        playbook = _lock_playbook(tenant, playbook_pk)
        _require_actor(actor, tenant=tenant, system_row=playbook.is_system)
        version = playbook.versions.filter(number=number).first()
        if version is None:
            msg = f"Version {number} does not exist on {playbook.key}."
            raise PlaybookVersion.DoesNotExist(msg)
        if version.state != VersionState.DRAFT:
            msg = "Only a draft version can be previewed for publishing."
            raise IllegalTransition(msg)
        normalized = validate_draft(
            applies_to=version.applies_to_json,
            steps=_specs_of_version(version),
            known_obligations=_known_obligations(),
        )
        return _compute_preview(
            tenant=tenant,
            playbook=playbook,
            version=version,
            normalized=normalized,
            on=on,
        )


def publish_version(
    *,
    actor: User | None,
    tenant: "Tenant",
    playbook_pk: UUID,
    number: int,
    expected_version: int,
) -> tuple[PlaybookVersion, PublishPreview]:
    """Publish a draft: validate fully, then move `state` under the lock.

    Returns the version and the preview computed under the same lock, so the
    numbers the publisher confirmed are the numbers the publish ran with. The
    state move is a guarded `UPDATE`, not `save()` — the model's own guard
    refuses it otherwise, which is the mechanism that keeps every other code
    path from reaching `published` without this validation.
    """
    with tenant_context(tenant.pk):
        playbook = _lock_playbook(tenant, playbook_pk)
        _require_actor(actor, tenant=tenant, system_row=playbook.is_system)
        version = _locked_version(playbook, number, expected_version)
        if version.state != VersionState.DRAFT:
            msg = f"Version {number} is {version.state}; only drafts publish."
            raise IllegalTransition(msg)
        # The same validation preview ran, run again against the locked row:
        # the preview that justified this publish may be older than the last
        # edit, and "validate then publish" must not become "validated once".
        normalized = validate_draft(
            applies_to=version.applies_to_json,
            steps=_specs_of_version(version),
            known_obligations=_known_obligations(),
        )
        preview = _compute_preview(
            tenant=tenant,
            playbook=playbook,
            version=version,
            normalized=normalized,
            on=None,
        )
        # ALL_OBJECTS_OK: pk-scoped guarded state move — save() refuses it
        # on purpose, so the transition exists only here.
        PlaybookVersion.all_objects.filter(pk=version.pk).update(
            state=VersionState.PUBLISHED,
            published_at=timezone.now(),
            published_by=actor,
            applies_to_json=normalized,
            version=version.version + 1,
        )
        version.refresh_from_db()
        record_event(
            action=AuditAction.PLAYBOOK_PUBLISHED,
            tenant_id=tenant.pk,
            actor=actor,
            obj=ObjectRef(type="PlaybookVersion", id=str(version.pk)),
            metadata={
                "playbook": str(playbook.pk),
                "number": version.number,
                "matching_clients": preview.matching_clients,
                "pinned_open_cycles": preview.pinned_open_cycles,
            },
        )
    return version, preview


def retire_version(
    *,
    actor: User | None,
    tenant: "Tenant",
    playbook_pk: UUID,
    number: int,
    expected_version: int,
) -> PlaybookVersion:
    """Retire a published version — cycles pinned to it are untouched.

    Drafts are deleted, not retired; publishing again would resurrect a retired
    version's number sequence only by `create_draft_version`, which is correct:
    a retired row stays readable as the record generated cycles point at.
    """
    with tenant_context(tenant.pk):
        playbook = _lock_playbook(tenant, playbook_pk)
        _require_actor(actor, tenant=tenant, system_row=playbook.is_system)
        version = _locked_version(playbook, number, expected_version)
        if version.state != VersionState.PUBLISHED:
            msg = f"Version {number} is {version.state}; only published retires."
            raise IllegalTransition(msg)
        # ALL_OBJECTS_OK: pk-scoped guarded state move; same reason as
        # publish.
        PlaybookVersion.all_objects.filter(pk=version.pk).update(
            state=VersionState.RETIRED,
            version=version.version + 1,
        )
        version.refresh_from_db()
        record_event(
            action=AuditAction.PLAYBOOK_RETIRED,
            tenant_id=tenant.pk,
            actor=actor,
            obj=ObjectRef(type="PlaybookVersion", id=str(version.pk)),
            metadata={
                "playbook": str(playbook.pk),
                "number": version.number,
            },
        )
    return version


__all__ = [
    "OPEN_CYCLE_STATES",
    "PublishPreview",
    "clone_playbook",
    "create_draft_version",
    "create_playbook",
    "move_step",
    "preview_publish",
    "publish_version",
    "retire_version",
    "update_draft",
]
