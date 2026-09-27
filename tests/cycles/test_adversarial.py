"""Adversarial probes beyond the acceptance tests — manual-review material.

Each probe is a shape the acceptance suite does not name: a disabled step as a
dependency target, a duplicate key collapse, a second publish, the update()
bypass the save() guard cannot cover, a system-row edit by a tenant account,
and a foreign-pk clone attempt. Printed results are captured into
.evidence/AXR1-008-adversarial.txt; the asserts are the parts that must hold.
"""

from http import HTTPStatus

import pytest
from django.core.exceptions import PermissionDenied

from apps.core.tenancy import tenant_context
from apps.cycles.errors import (
    IllegalTransition,
    ImmutableVersion,
    PlaybookValidationError,
    VersionConflict,
)
from apps.cycles.models import Playbook, PlaybookVersion, VersionState
from apps.cycles.packs import install_system_packs
from apps.cycles.rules import StepSpec
from apps.cycles.services import playbooks
from tests.ui.factories import Firm, make_firm

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def firm() -> Firm:
    """A firm without the flag — pack installation needs the flagged fixture."""
    return make_firm("adv-base")


def _spec(key: str, **over: object) -> StepSpec:
    fields: dict[str, object] = {
        "key": key,
        "kind": "task",
        "label_ptbr": f"Etapa {key}",
        "due_rule": {"kind": "period_end_offset", "days": 5},
    }
    fields.update(over)
    return StepSpec(**fields)  # type: ignore[arg-type]


def test_a_dependency_may_name_a_disabled_step() -> None:
    """Deps may point at disabled steps — the DAG is about order, not reachability."""
    firm = make_firm("adv-dep")
    with tenant_context(firm.tenant.id):
        playbook = playbooks.create_playbook(
            actor=firm.owner,
            tenant=firm.tenant,
            key="dep-on-off",
            name="Dep on off",
            steps=[
                _spec("a", is_enabled=False),
                _spec("b", depends_on=("a",)),
            ],
        )
        version = playbook.versions.get(number=1)
        assert version.steps.count() == 2


def test_a_duplicate_step_key_is_refused() -> None:
    firm = make_firm("adv-dup")
    with (
        tenant_context(firm.tenant.id),
        pytest.raises(PlaybookValidationError),
    ):
        playbooks.create_playbook(
            actor=firm.owner,
            tenant=firm.tenant,
            key="dup",
            name="Dup",
            steps=[_spec("x"), _spec("x")],
        )


def test_a_second_publish_is_refused(firm: Firm) -> None:
    with tenant_context(firm.tenant.id):
        playbook = playbooks.create_playbook(
            actor=firm.owner,
            tenant=firm.tenant,
            key="double-pub",
            name="Double",
            steps=[_spec("s")],
        )
        version, _ = playbooks.publish_version(
            actor=firm.owner,
            tenant=firm.tenant,
            playbook_pk=playbook.pk,
            number=1,
            expected_version=1,
        )
        with pytest.raises(IllegalTransition):
            playbooks.publish_version(
                actor=firm.owner,
                tenant=firm.tenant,
                playbook_pk=playbook.pk,
                number=1,
                expected_version=version.version,
            )


def test_update_bypasses_the_save_guard_by_design(firm: Firm) -> None:
    """QuerySet.update() cannot run save() — document the hole and its cover.

    The in-model guard exists so a stray `save()` cannot reach `published`; the
    deliberate `UPDATE` path lives only inside this domain's services, each of
    which re-runs the lock, validation and audit. This probe exists so the
    bypass is a named, tested fact rather than an accident of implementation.
    """
    with tenant_context(firm.tenant.id):
        playbook = playbooks.create_playbook(
            actor=firm.owner,
            tenant=firm.tenant,
            key="upd",
            name="Upd",
            steps=[_spec("s")],
        )
        version, _ = playbooks.publish_version(
            actor=firm.owner,
            tenant=firm.tenant,
            playbook_pk=playbook.pk,
            number=1,
            expected_version=1,
        )
        # The bypass works at the SQL layer — that is exactly why the
        # transition contract lives in the services and not on the row alone.
        # ALL_OBJECTS_OK: the probe itself asserts this bypass exists.
        written = PlaybookVersion.all_objects.filter(pk=version.pk).update(
            state=VersionState.DRAFT
        )
        assert written == 1
        # ALL_OBJECTS_OK: restore the published state the test set up.
        PlaybookVersion.all_objects.filter(pk=version.pk).update(
            state=VersionState.PUBLISHED
        )


def test_a_tenant_account_cannot_draft_a_system_pack() -> None:
    flagged = make_firm("adv-flag")
    flagged.tenant.features = ["cycles_r1"]
    flagged.tenant.save(update_fields=["features"])
    install_system_packs(flagged.tenant)
    with tenant_context(flagged.tenant.id):
        pack = Playbook.objects.get(key="mei-mensal")
        with pytest.raises(PermissionDenied):
            playbooks.create_draft_version(
                actor=flagged.owner,
                tenant=flagged.tenant,
                playbook_pk=pack.pk,
            )


def test_a_foreign_pk_is_a_missing_row_not_a_leak() -> None:
    firm = make_firm("adv-firm")
    other = make_firm("adv-other")
    with (
        tenant_context(firm.tenant.id),
        pytest.raises(Playbook.DoesNotExist),
    ):
        playbooks.clone_playbook(
            actor=firm.owner,
            tenant=firm.tenant,
            playbook_pk=other.tenant.pk,  # a valid UUID, wrong table
        )
    # And a real foreign playbook pk: also invisible, not PermissionDenied.
    with tenant_context(other.tenant.id):
        foreign = playbooks.create_playbook(
            actor=other.owner,
            tenant=other.tenant,
            key="foreign",
            name="Foreign",
            steps=[_spec("s")],
        )
    with (
        tenant_context(firm.tenant.id),
        pytest.raises(Playbook.DoesNotExist),
    ):
        playbooks.clone_playbook(
            actor=firm.owner,
            tenant=firm.tenant,
            playbook_pk=foreign.pk,
        )


def test_the_installer_identity_is_confined_to_system_rows() -> None:
    firm = make_firm("adv-installer")
    with tenant_context(firm.tenant.id):
        playbook = playbooks.create_playbook(
            actor=firm.owner,
            tenant=firm.tenant,
            key="mine",
            name="Mine",
            steps=[_spec("s")],
        )
        # actor=None is the installer identity: confined to is_system rows, it
        # refuses a tenant row with the same guard that makes a pack immutable.
        with pytest.raises(ImmutableVersion):
            playbooks.update_draft(
                actor=None,
                tenant=firm.tenant,
                playbook_pk=playbook.pk,
                number=1,
                expected_version=1,
                steps=[_spec("t")],
            )
        assert playbook.versions.get(number=1).steps.get().key == "s"


def test_a_retired_version_cannot_be_republished(firm: Firm) -> None:
    with tenant_context(firm.tenant.id):
        playbook = playbooks.create_playbook(
            actor=firm.owner,
            tenant=firm.tenant,
            key="ret",
            name="Ret",
            steps=[_spec("s")],
        )
        version, _ = playbooks.publish_version(
            actor=firm.owner,
            tenant=firm.tenant,
            playbook_pk=playbook.pk,
            number=1,
            expected_version=1,
        )
        retired = playbooks.retire_version(
            actor=firm.owner,
            tenant=firm.tenant,
            playbook_pk=playbook.pk,
            number=1,
            expected_version=version.version,
        )
        with pytest.raises(IllegalTransition):
            playbooks.publish_version(
                actor=firm.owner,
                tenant=firm.tenant,
                playbook_pk=playbook.pk,
                number=1,
                expected_version=retired.version,
            )
        with pytest.raises(VersionConflict):
            # a stale counter read BEFORE the retire loses the race loudly
            playbooks.retire_version(
                actor=firm.owner,
                tenant=firm.tenant,
                playbook_pk=playbook.pk,
                number=1,
                expected_version=version.version,
            )


def test_the_formset_extra_row_never_becomes_a_ghost_step(firm: Firm) -> None:
    """A bound formset's blank extra row must parse to nothing, not a step."""
    from apps.cycles.forms import StepFormSet  # noqa: PLC0415

    with tenant_context(firm.tenant.id):
        playbook = playbooks.create_playbook(
            actor=firm.owner,
            tenant=firm.tenant,
            key="ghost",
            name="Ghost",
            steps=[_spec("s")],
        )
        version = playbook.versions.get(number=1)
        formset = StepFormSet(
            {
                "steps-TOTAL_FORMS": "2",
                "steps-INITIAL_FORMS": "1",
                "steps-MIN_NUM_FORMS": "0",
                "steps-MAX_NUM_FORMS": "1000",
                "steps-0-key": "s",
                "steps-0-kind": "task",
                "steps-0-label_ptbr": "S",
                "steps-0-owner_rule": "team",
                "steps-0-due_kind": "period_end_offset",
                "steps-0-due_day": "5",
                # the blank extra row Django renders for adding a step:
            },
            prefix="steps",
        )
        assert formset.is_valid()
        from apps.cycles.views import _specs_of_valid_formset  # noqa: PLC0415

        specs = _specs_of_valid_formset(formset)
        assert [s.key for s in specs] == ["s"]
        assert version.steps.count() == 1


def test_reparenting_a_published_step_is_refused(firm: Firm) -> None:
    """B1: a step's PERSISTED parent, not the in-memory FK, decides legality.

    Before the guard this save() succeeded and silently removed the step from
    the published version — the immutability contract broken through the FK.
    """
    from apps.cycles.errors import ImmutableVersion  # noqa: PLC0415

    with tenant_context(firm.tenant.id):
        source = playbooks.create_playbook(
            actor=firm.owner,
            tenant=firm.tenant,
            key="reparent-src",
            name="Source",
            steps=[_spec("a"), _spec("b")],
        )
        playbooks.publish_version(
            actor=firm.owner,
            tenant=firm.tenant,
            playbook_pk=source.pk,
            number=1,
            expected_version=1,
        )
        target = playbooks.create_playbook(
            actor=firm.owner,
            tenant=firm.tenant,
            key="reparent-dst",
            name="Target",
            steps=[_spec("c")],
        )
        published = source.versions.get(number=1)
        draft = target.versions.get(number=1)
        assert published.steps.count() == 2

        step = published.steps.get(key="a")
        step.version = draft  # repoint at an empty-allowed DRAFT parent
        with pytest.raises(ImmutableVersion):
            step.save()
        # The published source never lost the member.
        assert published.steps.count() == 2

        # Moving a draft step INTO a published version is refused the same way.
        draft_step = draft.steps.get(key="c")
        draft_step.version = published
        with pytest.raises(ImmutableVersion):
            draft_step.save()
        assert draft.steps.count() == 1
        assert published.steps.count() == 2

        # And the row-level delete on the published parent is still refused.
        with pytest.raises(ImmutableVersion):
            published.steps.get(key="b").delete()
        assert published.steps.count() == 2


def test_concurrent_pack_installs_do_not_duplicate_or_crash() -> None:
    """Two installers on one tenant must converge: one inserts, one re-reads.

    The barrier sits INSIDE `Playbook.objects.create` — the narrowest critical
    section — so both transactions hold an uncommitted intent to insert the same
    pack key at the same moment; the loser then provably walks the
    IntegrityError re-fetch instead of merely racing a wider window. A barrier
    timeout is swallowed: if the winner committed before the loser even
    filtered, the serialized path is already correct and nothing was forced.
    """
    import contextlib  # noqa: PLC0415
    import threading  # noqa: PLC0415
    from threading import Barrier, BrokenBarrierError  # noqa: PLC0415
    from unittest.mock import patch  # noqa: PLC0415

    from tests.support_threads import run_in_thread_as_runtime  # noqa: PLC0415

    flagged = make_firm("adv-race")
    # update() bypasses post_save: the packs are NOT installed yet, so both
    # threads race to create them rather than finding finished rows.
    flagged.tenant.__class__.objects.filter(pk=flagged.tenant.pk).update(
        features=["cycles_r1"]
    )
    flagged.tenant.refresh_from_db()
    barrier = Barrier(2)
    real_create = Playbook.objects.create

    def gated_create(*args: object, **kwargs: object) -> Playbook:
        # Gate only the FIRST pack's create — the barrier is for the racing
        # partner, and a second pack from the same thread would trip it alone.
        if kwargs.get("key") == "mei-mensal":
            # partner may serialize before reaching create — nothing to race
            with contextlib.suppress(BrokenBarrierError):
                barrier.wait(timeout=8)
        return real_create(*args, **kwargs)

    def install() -> int:
        return len(install_system_packs(flagged.tenant))

    # The helper joins its worker, so it cannot be the main thread's half of the
    # race: wrap it in a plain thread, and let THIS thread be the other racer.
    box: dict[str, object] = {}

    def worker() -> None:
        try:
            box["count"] = run_in_thread_as_runtime(flagged.tenant.id, install)
        except BaseException as exc:  # noqa: BLE001 — re-raised below, not lost
            box["error"] = exc

    racing = threading.Thread(target=worker, name="installer-b", daemon=True)
    with patch.object(Playbook.objects, "create", side_effect=gated_create):
        racing.start()
        mine = install()
        racing.join(timeout=60)
    assert not racing.is_alive(), "the racing installer never finished"
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    assert mine == 2
    assert box["count"] == 2
    # Exactly two system playbooks exist, each with exactly one version.
    with tenant_context(flagged.tenant.id):
        assert Playbook.objects.filter(is_system=True).count() == 2
        for playbook in Playbook.objects.filter(is_system=True):
            assert playbook.versions.count() == 1
            assert playbook.versions.get().state == VersionState.PUBLISHED


def test_a_foreign_playbook_pk_over_http_is_404_not_500() -> None:
    """The reviewer-caught hole: DoesNotExist escaped _list_post as a 500."""
    from django.urls import reverse  # noqa: PLC0415

    firm = make_firm("adv-404")
    firm.tenant.features = ["cycles_r1"]
    firm.tenant.save(update_fields=["features"])
    other = make_firm("adv-404b")
    with tenant_context(other.tenant.id):
        foreign = playbooks.create_playbook(
            actor=other.owner,
            tenant=other.tenant,
            key="foreign-pk",
            name="Foreign",
            steps=[_spec("s")],
        )
    session = firm.as_owner()
    for action in ("clone", "new_draft"):
        response = session.post(
            reverse("playbook-list"),
            data={"action": action, "playbook": str(foreign.pk)},
        )
        assert response.status_code == HTTPStatus.NOT_FOUND
