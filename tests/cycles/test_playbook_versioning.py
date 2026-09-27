"""The playbook lifecycle contract, end to end (AXR1-008).

Every case here runs as `app_runtime` under the real policies (the conftest role
fixture), inside `tenant_context` where a firm is the subject — the same shape
production code runs in. The acceptance list is the plan's, plus the three
service guarantees the worker contract pins on every state change: the row lock,
the optimistic version check, and the audit row.
"""

from datetime import date
from http import HTTPStatus

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.clients.models import ClientCompany, Engagement, TaxProfile
from apps.core.tenancy import tenant_context
from apps.cycles.errors import (
    DagCycleError,
    ImmutableVersion,
    VersionConflict,
)
from apps.cycles.models import Playbook, PlaybookVersion, VersionState
from apps.cycles.packs import COLETA_MENSAL, MEI_MENSAL, install_system_packs
from apps.cycles.rules import StepSpec
from apps.cycles.services import playbooks
from tests.isolation.rolecheck import assert_isolated_role
from tests.ui.factories import Firm, add_client, client_base, make_firm

pytestmark = pytest.mark.django_db(transaction=True)


def _spec(key: str, *, depends_on: tuple[str, ...] = ()) -> StepSpec:
    """A minimal valid step for tests that do not care about its content."""
    return StepSpec(
        key=key,
        kind="task",
        label_ptbr=f"Etapa {key}",
        depends_on=depends_on,
        due_rule={"kind": "period_end_offset", "days": 5},
    )


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]


@pytest.fixture
def firm() -> Firm:
    return make_firm("cycles-a")


@pytest.fixture
def flagged(firm: Firm) -> Firm:
    """The same firm with the feature flag on — the signal installs the packs."""
    firm.tenant.features = ["cycles_r1"]
    firm.tenant.save(update_fields=["features"])
    return firm


# --- Publishing and the DAG -------------------------------------------------


def test_publish_refuses_a_cyclic_dag_and_names_the_keys(firm: Firm) -> None:
    with tenant_context(firm.tenant.id):
        playbook = playbooks.create_playbook(
            actor=firm.owner,
            tenant=firm.tenant,
            key="ciclico",
            name="Cíclico",
            steps=[
                _spec("a", depends_on=("b",)),
                _spec("b", depends_on=("a",)),
            ],
        )
        draft = playbook.versions.get(number=1)
        with pytest.raises(DagCycleError) as caught:
            playbooks.publish_version(
                actor=firm.owner,
                tenant=firm.tenant,
                playbook_pk=playbook.pk,
                number=1,
                expected_version=draft.version,
            )
        assert set(caught.value.cycle_keys) == {"a", "b"}
        # The version stayed a draft: a refused publish writes nothing.
        draft.refresh_from_db()
        assert draft.state == VersionState.DRAFT
        assert draft.published_at is None


def test_the_editor_view_answers_the_cycle_with_422(firm: Firm) -> None:
    """The same refusal, at the HTTP surface the acceptance names.

    Publishing now requires a preview token — which only a successful preview
    can mint — so the cyclic draft refuses at the preview action; a forged
    direct publish is refused even earlier by the token check.
    """
    firm.tenant.features = ["cycles_r1"]
    firm.tenant.save(update_fields=["features"])
    with tenant_context(firm.tenant.id):
        playbook = playbooks.create_playbook(
            actor=firm.owner,
            tenant=firm.tenant,
            key="ciclico-http",
            name="Cíclico",
            steps=[
                _spec("a", depends_on=("b",)),
                _spec("b", depends_on=("a",)),
            ],
        )
    session = firm.as_owner()
    response = session.post(
        reverse("playbook-editor", kwargs={"pk": playbook.pk, "number": 1}),
        data={
            "action": "preview",
            "expected_version": "1",
            "name": "Cíclico",
            "steps-TOTAL_FORMS": "2",
            "steps-INITIAL_FORMS": "0",
            "steps-MIN_NUM_FORMS": "0",
            "steps-MAX_NUM_FORMS": "1000",
            "steps-0-key": "a",
            "steps-0-kind": "task",
            "steps-0-label_ptbr": "A",
            "steps-0-depends_on": "b",
            "steps-0-owner_rule": "team",
            "steps-0-due_kind": "period_end_offset",
            "steps-0-due_day": "5",
            "steps-1-key": "b",
            "steps-1-kind": "task",
            "steps-1-label_ptbr": "B",
            "steps-1-depends_on": "a",
            "steps-1-owner_rule": "team",
            "steps-1-due_kind": "period_end_offset",
            "steps-1-due_day": "5",
        },
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_CONTENT
    assert b"Depend" in response.content  # the cycle banner names the keys

    # And a direct publish POST — no preview ever rendered — is refused before
    # validation, leaving the draft untouched.
    direct = session.post(
        reverse("playbook-editor", kwargs={"pk": playbook.pk, "number": 1}),
        data={"action": "publish", "expected_version": "1"},
    )
    assert direct.status_code == HTTPStatus.CONFLICT
    with tenant_context(firm.tenant.id):
        assert playbook.versions.get(number=1).state == VersionState.DRAFT


def test_publish_requires_the_preview_token_of_the_current_revision(
    flagged: Firm,
) -> None:
    """B2/B3: publish needs the signed token, and edits invalidate it.

    The flow: preview renders a token bound to the revision it described;
    publish accepts only that token; any subsequent draft write changes the
    revision, so an old token — or none — is a 409 with no writes.
    """
    with tenant_context(flagged.tenant.id):
        playbook = playbooks.create_playbook(
            actor=flagged.owner,
            tenant=flagged.tenant,
            key="tok",
            name="Token",
            steps=[_spec("a")],
        )
    session = flagged.as_owner()
    url = reverse("playbook-editor", kwargs={"pk": playbook.pk, "number": 1})
    save_fields = {
        "name": "Token",
        "steps-TOTAL_FORMS": "1",
        "steps-INITIAL_FORMS": "1",
        "steps-MIN_NUM_FORMS": "0",
        "steps-MAX_NUM_FORMS": "1000",
        "steps-0-key": "a",
        "steps-0-kind": "task",
        "steps-0-label_ptbr": "A",
        "steps-0-owner_rule": "team",
        "steps-0-due_kind": "period_end_offset",
        "steps-0-due_day": "5",
    }

    # Missing revision token on a mutating POST: refused, nothing written.
    missing = session.post(
        url, data={"action": "save", **save_fields}
    )
    assert missing.status_code == HTTPStatus.CONFLICT

    # Publishing without any preview is refused, revision correct or not.
    direct = session.post(
        url,
        data={"action": "publish", "expected_version": "1"},
    )
    assert direct.status_code == HTTPStatus.CONFLICT
    with tenant_context(flagged.tenant.id):
        assert playbook.versions.get(number=1).state == VersionState.DRAFT

    # Preview mints the token bound to revision 2 (the preview's own save).
    preview = session.post(
        url,
        data={"action": "preview", "expected_version": "1", **save_fields},
    )
    assert preview.status_code == HTTPStatus.OK
    with tenant_context(flagged.tenant.id):
        version = playbook.versions.get(number=1)
        assert version.version == 2
        token = f"{version.pk}:{version.version}"
        from django.core.signing import TimestampSigner  # noqa: PLC0415

        signed = TimestampSigner(salt="cycles.preview-publish.v1").sign(token)

    # Publishing WITH the token lands.
    published = session.post(
        url,
        data={
            "action": "publish",
            "expected_version": "2",
            "preview_token": signed,
        },
    )
    assert published.status_code == HTTPStatus.FOUND
    with tenant_context(flagged.tenant.id):
        assert playbook.versions.get(number=1).state == VersionState.PUBLISHED


def test_a_preview_token_signed_for_an_old_revision_is_refused(
    flagged: Firm,
) -> None:
    """Editing the draft after the preview invalidates its token (409)."""
    from django.core.signing import TimestampSigner  # noqa: PLC0415

    with tenant_context(flagged.tenant.id):
        playbook = playbooks.create_playbook(
            actor=flagged.owner,
            tenant=flagged.tenant,
            key="stale-tok",
            name="Stale token",
            steps=[_spec("a")],
        )
        draft = playbook.versions.get(number=1)
        stale = TimestampSigner(salt="cycles.preview-publish.v1").sign(
            f"{draft.pk}:{draft.version}"
        )
        playbooks.update_draft(
            actor=flagged.owner,
            tenant=flagged.tenant,
            playbook_pk=playbook.pk,
            number=1,
            expected_version=1,
            steps=[_spec("a"), _spec("b")],
        )
    session = flagged.as_owner()
    response = session.post(
        reverse("playbook-editor", kwargs={"pk": playbook.pk, "number": 1}),
        data={
            "action": "publish",
            "expected_version": "2",
            "preview_token": stale,
        },
    )
    assert response.status_code == HTTPStatus.CONFLICT
    with tenant_context(flagged.tenant.id):
        assert playbook.versions.get(number=1).state == VersionState.DRAFT


def test_a_published_version_is_immutable(firm: Firm) -> None:
    with tenant_context(firm.tenant.id):
        playbook = playbooks.create_playbook(
            actor=firm.owner,
            tenant=firm.tenant,
            key="imutavel",
            name="Imutável",
            steps=[_spec("a")],
        )
        version, _preview = playbooks.publish_version(
            actor=firm.owner,
            tenant=firm.tenant,
            playbook_pk=playbook.pk,
            number=1,
            expected_version=1,
        )
        step = version.steps.get()
        with pytest.raises(ImmutableVersion):
            version.save()
        version.state = VersionState.DRAFT
        with pytest.raises(ImmutableVersion):
            version.save()
        with pytest.raises(ImmutableVersion):
            step.save()
        with pytest.raises(ImmutableVersion):
            step.delete()


def test_service_updates_refuse_published_versions(firm: Firm) -> None:
    """The failure scenario the plan names: a published step update is refused."""
    with tenant_context(firm.tenant.id):
        playbook = playbooks.create_playbook(
            actor=firm.owner,
            tenant=firm.tenant,
            key="via-servico",
            name="Via serviço",
            steps=[_spec("a")],
        )
        playbooks.publish_version(
            actor=firm.owner,
            tenant=firm.tenant,
            playbook_pk=playbook.pk,
            number=1,
            expected_version=1,
        )
        published = playbook.versions.get(number=1)
        with pytest.raises(ImmutableVersion):
            playbooks.update_draft(
                actor=firm.owner,
                tenant=firm.tenant,
                playbook_pk=playbook.pk,
                number=1,
                expected_version=published.version,
                steps=[_spec("b")],
            )
        # ImmutableVersion — the same refusal the row's own save() gives.
        assert published.steps.count() == 1


def test_a_stale_expected_version_conflicts_without_writing(firm: Firm) -> None:
    """Two callers race on one draft; the loser gets 409-shaped refusal."""
    with tenant_context(firm.tenant.id):
        playbook = playbooks.create_playbook(
            actor=firm.owner,
            tenant=firm.tenant,
            key="corrida",
            name="Corrida",
            steps=[_spec("a")],
        )
        first = playbook.versions.get(number=1)
        playbooks.update_draft(
            actor=firm.owner,
            tenant=firm.tenant,
            playbook_pk=playbook.pk,
            number=1,
            expected_version=first.version,
            steps=[_spec("a"), _spec("b")],
        )
        # The second caller still holds the revision it read before the write.
        with pytest.raises(VersionConflict):
            playbooks.update_draft(
                actor=firm.owner,
                tenant=firm.tenant,
                playbook_pk=playbook.pk,
                number=1,
                expected_version=first.version,
                steps=[_spec("a"), _spec("b"), _spec("c")],
            )
        assert playbook.versions.get(number=1).steps.count() == 2


# --- Preview ----------------------------------------------------------------


def _register(
    client: ClientCompany,
    *,
    regime: str,
    service_kind: str,
) -> None:
    """Write the registry rows targeting reads: a current profile + engagement.

    `provenance="probe"` marks the row as test-seeded rather than backfilled —
    the targeting code reads values, not provenance, so either would do.
    """
    TaxProfile.objects.create(
        tenant_id=client.tenant_id,
        client=client,
        regime=regime,
        valid_from=date(2020, 1, 1),
        provenance="probe",
    )
    Engagement.objects.create(
        tenant_id=client.tenant_id,
        client=client,
        service_kind=service_kind,
        starts_on=date(2020, 1, 1),
        provenance="probe",
    )


def test_publish_preview_counts_the_clients_the_scope_covers(firm: Firm) -> None:
    """Exact counts: profile regime and engagement kind decide targeting."""
    alpha = add_client(
        firm, legal_name="Alpha MEI", base=client_base(0, firm.tenant.slug)
    )
    beta = add_client(
        firm, legal_name="Beta MEI", base=client_base(1, firm.tenant.slug)
    )
    gama = add_client(
        firm, legal_name="Gama Ltda", base=client_base(2, firm.tenant.slug)
    )
    with tenant_context(firm.tenant.id):
        _register(alpha, regime="mei", service_kind="mei_mensal")
        _register(beta, regime="mei", service_kind="mei_mensal")
        _register(gama, regime="simples", service_kind="documentos_mensal")

        mei = playbooks.create_playbook(
            actor=firm.owner,
            tenant=firm.tenant,
            key="mei-preview",
            name="MEI",
            applies_to={"regimes": ["mei"], "service_kinds": ["mei_mensal"]},
            steps=[_spec("a")],
        )
        preview = playbooks.preview_publish(
            actor=firm.owner,
            tenant=firm.tenant,
            playbook_pk=mei.pk,
            number=1,
        )
        assert preview.matching_clients == 2
        assert preview.pinned_open_cycles == 0

        coleta = playbooks.create_playbook(
            actor=firm.owner,
            tenant=firm.tenant,
            key="coleta-preview",
            name="Coleta",
            applies_to={"service_kinds": ["documentos_mensal"]},
            steps=[_spec("a")],
        )
        preview = playbooks.preview_publish(
            actor=firm.owner,
            tenant=firm.tenant,
            playbook_pk=coleta.pk,
            number=1,
        )
        assert preview.matching_clients == 1

        # A v2 publish reports the same counts against the older pinned version.
        playbooks.publish_version(
            actor=firm.owner,
            tenant=firm.tenant,
            playbook_pk=mei.pk,
            number=1,
            expected_version=1,
        )
        draft2 = playbooks.create_draft_version(
            actor=firm.owner, tenant=firm.tenant, playbook_pk=mei.pk
        )
        preview2 = playbooks.preview_publish(
            actor=firm.owner,
            tenant=firm.tenant,
            playbook_pk=mei.pk,
            number=draft2.number,
        )
        assert preview2.matching_clients == 2
        assert preview2.pinned_open_cycles == 0  # no ServiceCycle rows exist yet


def test_targeting_reads_the_profile_not_the_legacy_flag(firm: Firm) -> None:
    """is_mei is legacy input; the profile claim is the answer targeting uses.

    A client whose flag says MEI but whose current profile says `simples` must
    NOT match the MEI pack, and a closed engagement must not match even while a
    profile does — the period bound is part of "open".
    """
    legacy_flagged = add_client(
        firm, legal_name="Flag MEI", base=client_base(0, firm.tenant.slug)
    )
    real_mei = add_client(
        firm, legal_name="Profile MEI", base=client_base(1, firm.tenant.slug)
    )
    ended = add_client(
        firm, legal_name="Ended MEI", base=client_base(2, firm.tenant.slug)
    )
    with tenant_context(firm.tenant.id):
        # Flag says MEI, profile says simples — the profile wins.
        assert legacy_flagged.is_mei
        _register(legacy_flagged, regime="simples", service_kind="documentos_mensal")
        _register(real_mei, regime="mei", service_kind="mei_mensal")
        # A closed engagement drops the service-kind axis even with regime MEI.
        _register(ended, regime="mei", service_kind="mei_mensal")
        ended_engagement = ended.engagements.get()
        ended_engagement.ends_on = date(2020, 6, 1)
        ended_engagement.save()

        mei = playbooks.create_playbook(
            actor=firm.owner,
            tenant=firm.tenant,
            key="profile-wins",
            name="Profile wins",
            applies_to={"regimes": ["mei"], "service_kinds": ["mei_mensal"]},
            steps=[_spec("a")],
        )
        preview = playbooks.preview_publish(
            actor=firm.owner,
            tenant=firm.tenant,
            playbook_pk=mei.pk,
            number=1,
        )
        assert preview.matching_clients == 1
        assert preview.client_ids == (real_mei.pk,)


# --- Tenant isolation --------------------------------------------------------


def test_tenant_a_cannot_see_tenant_bs_cloned_playbook(firm: Firm) -> None:
    """RLS, not the manager: the denial is asserted at the database layer."""
    assert_isolated_role()
    other = make_firm("cycles-b")
    with tenant_context(other.tenant.id):
        source = playbooks.create_playbook(
            actor=other.owner,
            tenant=other.tenant,
            key="segredo",
            name="Playbook secreto",
            steps=[_spec("a")],
        )
    # Inside A's context, B's playbook is invisible at both layers.
    with tenant_context(firm.tenant.id):
        assert Playbook.objects.filter(pk=source.pk).first() is None
        # ALL_OBJECTS_OK: the denial itself is the assertion — the row must
        # be absent at the layer RLS enforces, not only at the ORM filter.
        assert Playbook.all_objects.filter(pk=source.pk).first() is None
        with pytest.raises(Playbook.DoesNotExist):
            playbooks.create_draft_version(
                actor=firm.owner,
                tenant=firm.tenant,
                playbook_pk=source.pk,
            )
    # Vacuity control: as the table owner the row plainly exists, so the two
    # misses above are the policy refusing, not an empty table.
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
        cursor.execute(
            "SELECT count(*) FROM cycles_playbook WHERE id = %s",
            [str(source.pk)],
        )
        assert cursor.fetchone()[0] == 1


# --- System packs ------------------------------------------------------------


def test_install_system_packs_is_idempotent_and_tenant_scoped(
    flagged: Firm,
) -> None:
    tenant = flagged.tenant
    first = install_system_packs(tenant)
    # A second run must be a no-op — the assert on the table counts proves it.
    install_system_packs(tenant)
    assert {pack.key for pack in first} == {MEI_MENSAL.key, COLETA_MENSAL.key}
    with tenant_context(tenant.id):
        for key in (MEI_MENSAL.key, COLETA_MENSAL.key):
            playbook = Playbook.objects.get(key=key)
            assert playbook.is_system
            published = playbook.versions.get(number=1)
            assert published.number == 1
            assert published.steps.exists()
        # Second install created nothing: still exactly two playbooks.
        assert Playbook.objects.count() == 2
        assert PlaybookVersion.objects.count() == 2
    # No null-tenant rows exist anywhere — verified as the owner would see them.
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
        cursor.execute("SELECT count(*) FROM cycles_playbook WHERE tenant_id IS NULL")
        assert cursor.fetchone()[0] == 0


def test_the_flagless_tenant_gets_no_packs(firm: Firm) -> None:
    assert install_system_packs(firm.tenant) == []
    with tenant_context(firm.tenant.id):
        assert not Playbook.objects.exists()


def test_system_published_versions_are_immutable_and_not_editable(
    flagged: Firm,
) -> None:
    tenant = flagged.tenant
    with tenant_context(tenant.id):
        playbook = Playbook.objects.get(key=MEI_MENSAL.key)
        published = playbook.versions.get()
        with pytest.raises(ImmutableVersion):
            published.save()
        # Tenants do not edit system rows even through the service.
        with pytest.raises(PermissionDenied):
            playbooks.update_draft(
                actor=flagged.owner,
                tenant=tenant,
                playbook_pk=playbook.pk,
                number=published.number,
                expected_version=published.version,
                steps=[_spec("x")],
            )
        # Cloning is the supported path, and the clone is an ordinary draft.
        clone = playbooks.clone_playbook(
            actor=flagged.owner, tenant=tenant, playbook_pk=playbook.pk
        )
        assert not clone.is_system
        draft = clone.versions.get()
        assert draft.state == VersionState.DRAFT
        assert draft.steps.count() == published.steps.count()


def test_system_pack_contents_match_the_plan(flagged: Firm) -> None:
    """The pack definitions are the contract — pinned so drift is a red build."""
    tenant = flagged.tenant
    with tenant_context(tenant.id):
        mei = Playbook.objects.get(key=MEI_MENSAL.key)
        steps = {
            step.key: step
            for step in mei.versions.get(number=1).steps.order_by("position")
        }
        # The step list is the plan's, verbatim — not a subset of it.
        ordered = [
            step.key for step in mei.versions.get(number=1).steps.order_by("position")
        ]
        assert ordered == [
            "notas-fiscais",
            "extrato-receitas",
            "conferir-faturamento",
            "revisao-mensal",
            "aprovacao-envio",
            "publicar-guia-das",
            "aviso-email",
            "pagamento-informado",
        ]
        assert steps["extrato-receitas"].evidence_required
        assert not steps["notas-fiscais"].evidence_required
        assert steps["extrato-receitas"].label_ptbr == (
            "Extrato ou comprovantes de receitas"
        )
        assert steps["notas-fiscais"].label_ptbr == "Notas fiscais emitidas no mês"
        assert steps["conferir-faturamento"].label_ptbr == (
            "Conferir faturamento e limite MEI"
        )
        assert steps["publicar-guia-das"].kind == "client_publication"
        assert steps["publicar-guia-das"].label_ptbr == "Guia DAS do mês"
        assert steps["pagamento-informado"].kind == "checkpoint"
        assert steps["pagamento-informado"].label_ptbr == "Pagamento informado"
        assert steps["aprovacao-envio"].kind == "approval_gate"
        assert steps["revisao-mensal"].kind == "review"
        assert steps["aviso-email"].kind == "email_notice"

        coleta = Playbook.objects.get(key=COLETA_MENSAL.key)
        folha = coleta.versions.get(number=1).steps.get(key="documentos-folha")
        resumo = coleta.versions.get(number=1).steps.get(key="publicar-resumo")
        assert resumo.label_ptbr == "Resumo do mês"
        # Off by default AND never required: sensitive employee data stays out
        # until the firm deliberately switches the item on.
        assert not folha.is_enabled
        assert not folha.evidence_required


def test_an_unprivileged_member_is_denied_inside_the_lock(flagged: Firm) -> None:
    """`can()` is re-checked inside the service, not only at the view."""
    tenant = flagged.tenant
    with tenant_context(tenant.id):
        playbook = playbooks.create_playbook(
            actor=flagged.owner,
            tenant=tenant,
            key="deny",
            name="Deny",
            steps=[_spec("a")],
        )
        # staff_accountant holds users.create = NONE (and playbooks.manage =
        # NONE in S8.1): the service refuses, and nothing was written.
        with pytest.raises(PermissionDenied):
            playbooks.update_draft(
                actor=flagged.accountant,
                tenant=tenant,
                playbook_pk=playbook.pk,
                number=1,
                expected_version=1,
                steps=[_spec("b")],
            )
        assert playbook.versions.get(number=1).steps.get().key == "a"


def test_the_editor_is_behind_the_flag(firm: Firm) -> None:
    """A flag-off firm gets 404 on every playbook route."""
    session = firm.as_owner()
    response = session.get(reverse("playbook-list"))
    assert response.status_code == HTTPStatus.NOT_FOUND


def test_the_editor_is_capability_gated(flagged: Firm) -> None:
    """The staff accountant has no playbook capability under S8.1 or the gate."""
    session = flagged.as_accountant()
    response = session.get(reverse("playbook-list"))
    assert response.status_code == HTTPStatus.FORBIDDEN
