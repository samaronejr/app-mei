"""T14: owned exceptions, deterministic openers, waivers and portal visibility.

Real tenant graphs, real services and the real portal middleware (role switch, RLS,
MFA session). Races synchronize on `threading.Barrier` with bounded joins; no sleeps.
"""

import threading
from datetime import date, timedelta
from http import HTTPStatus
from typing import Final
from uuid import UUID, uuid4

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, connections, transaction
from django.http import Http404
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.audit.models import AuditAction, PlatformEvent
from apps.clients.maintenance import assignment_state, load_client, remove_assignment
from apps.clients.models import (
    ClientAssignment,
    ClientCompany,
    ClientContact,
    Engagement,
)
from apps.core.tenancy import tenant_context
from apps.cycles.errors import IllegalTransition, VersionConflict
from apps.cycles.models import (
    CycleEvent,
    EvidenceRequest,
    ExceptionInternal,
    ExceptionState,
    InternalNote,
    PlaybookVersion,
    RequestState,
    ServiceCycle,
)
from apps.cycles.models import Exception as CycleException
from apps.cycles.openers import (
    CERTIFICATE_EXPIRING,
    NO_RESPONSIBLE,
    REASONS,
    REQUEST_OVERDUE,
    TAX_PROFILE_UNKNOWN,
    evaluate_tenant_exceptions,
)
from apps.cycles.scanning import EXCEPTION_REASON as SCAN_UNAVAILABLE
from apps.cycles.services import cycle as cycle_services
from apps.cycles.services import exceptions
from apps.cycles.services.cycle import open_cycle
from apps.cycles.services.exceptions import (
    WAIVABLE_REASONS,
    assign_exception,
    open_exception,
    transition_exception,
)
from apps.cycles.tasks import open_deterministic_exceptions
from apps.delivery.kinds import INVITE_REJECTED
from apps.delivery.models import MessageState, OutboundMessage
from apps.delivery.tasks import dispatch_outbox
from apps.tenants.models import Invite, TenantRole
from tests.browser.harness import mfa_session_cookie
from tests.cycles.cycle_fixtures import PERIOD, CycleSetup, cycle_setup
from tests.delivery.notice_fixtures import acknowledge
from tests.delivery.smtp import FakeSMTP
from tests.portal.request_fixtures import portal_setup
from tests.support import enrol_totp
from tests.support_threads import RUNTIME_ROLE, run_in_thread_as_runtime

pytestmark = pytest.mark.django_db(transaction=True)

TODAY: Final = date(2026, 9, 10)
TIMEOUT: Final = 30.0
# Codes that must never be accepted as risk: uncertain external effect, security,
# provider refusals and the ownership prerequisite.
FORBIDDEN_WAIVERS: Final = (
    "delivery_unknown",
    "delivery_rejected",
    SCAN_UNAVAILABLE,
    INVITE_REJECTED,
    NO_RESPONSIBLE,
)


def _cycle(setup: CycleSetup, key: str = "t14") -> ServiceCycle:
    ref = open_cycle(
        setup.actor,
        setup.engagement.pk,
        PERIOD,
        setup.definition.pk,
        idempotency_key=key,
    )
    return ServiceCycle.objects.get(pk=ref.id)


def _open(
    setup: CycleSetup, cycle: ServiceCycle, reason: str, subject: UUID
) -> CycleException:
    cycle.refresh_from_db()
    return open_exception(
        reason,
        subject,
        actor=setup.actor,
        cycle_id=cycle.pk,
        expected_version=cycle.version,
        owner=setup.actor,
        review_on=PERIOD,
    )


def _firm(setup: CycleSetup) -> Client:
    enrol_totp(setup.actor)
    host = f"{setup.tenant.slug}.localhost"
    browser = Client(SERVER_NAME=host)
    cookie = mfa_session_cookie(setup.actor, f"http://{host}/")
    browser.cookies[cookie["name"]] = cookie["value"]
    return browser


def _writes() -> tuple[int, int, list[tuple[str, int, UUID]]]:
    """Event and note counts plus every exception's state/version/owner."""
    return (
        CycleEvent.objects.count(),
        InternalNote.objects.count(),
        list(
            CycleException.objects.order_by("pk").values_list(
                "state", "version", "owner_id"
            )
        ),
    )


def test_openers_open_each_rule_once_and_converge_on_rerun() -> None:
    setup = cycle_setup()
    with tenant_context(setup.tenant.pk):
        cycle = _cycle(setup)
        overdue = EvidenceRequest.objects.create(
            tenant=setup.tenant,
            client=setup.client,
            cycle=cycle,
            title="Notas de serviço",
            state=RequestState.REQUESTED,
            due_on=TODAY - timedelta(days=2),
            created_by=setup.actor,
            requested_at=timezone.now(),
        )
        EvidenceRequest.objects.create(
            tenant=setup.tenant,
            client=setup.client,
            cycle=cycle,
            title="Extrato já aceito",
            state=RequestState.ACCEPTED,
            due_on=TODAY - timedelta(days=2),
            created_by=setup.actor,
        )
        setup.client.certificate_expires_on = TODAY + timedelta(days=30)
        setup.client.save(update_fields=["certificate_expires_on"])
        Engagement.objects.filter(pk=setup.engagement.pk).update(responsible=None)

        evaluate_tenant_exceptions(setup.tenant, today=TODAY)
        rows = {row.reason_code: row for row in CycleException.objects.all()}
        assert set(rows) == {REQUEST_OVERDUE, CERTIFICATE_EXPIRING, NO_RESPONSIBLE}
        assert rows[REQUEST_OVERDUE].subject_ref == overdue.pk
        assert rows[CERTIFICATE_EXPIRING].due_on == TODAY + timedelta(days=30)
        assert rows[CERTIFICATE_EXPIRING].client_visible is True
        assert rows[CERTIFICATE_EXPIRING].client_message
        assert not rows[NO_RESPONSIBLE].client_visible
        assert {row.owner_id for row in rows.values()} == {setup.actor.pk}
        opened = CycleEvent.objects.filter(type="exception.opened").count()

        evaluate_tenant_exceptions(setup.tenant, today=TODAY)
        evaluate_tenant_exceptions(setup.tenant, today=TODAY + timedelta(days=1))
        assert CycleException.objects.count() == len(rows)
        assert CycleEvent.objects.filter(type="exception.opened").count() == opened

        # Outside the 30-day window nothing is opened for the certificate.
        CycleException.objects.filter(reason_code=CERTIFICATE_EXPIRING).update(
            state=ExceptionState.CANCELLED
        )
        setup.client.certificate_expires_on = TODAY + timedelta(days=31)
        setup.client.save(update_fields=["certificate_expires_on"])
        evaluate_tenant_exceptions(setup.tenant, today=TODAY)
        assert not CycleException.objects.filter(
            reason_code=CERTIFICATE_EXPIRING, state=ExceptionState.OPEN
        ).exists()


def test_a_cycle_without_a_tax_profile_opens_the_unknown_profile_exception() -> None:
    setup = cycle_setup()
    with tenant_context(setup.tenant.pk):
        other = ClientCompany.objects.create(
            tenant=setup.tenant,
            legal_name="Sem perfil",
            cnpj="11444777000161",
            status="active",
        )
        engagement = Engagement.objects.create(
            tenant=setup.tenant,
            client=other,
            service_kind="documentos_mensal",
            starts_on=PERIOD,
            responsible=setup.actor,
            provenance="staff_entry",
        )
        definition = PlaybookVersion.objects.get(
            playbook__key="coleta-mensal", state="published"
        )
        ref = open_cycle(
            setup.actor, engagement.pk, PERIOD, definition.pk, idempotency_key="gap"
        )
        _cycle(setup)
        evaluate_tenant_exceptions(setup.tenant, today=TODAY)
        assert list(CycleException.objects.values_list("cycle_id", "reason_code")) == [
            (ref.id, TAX_PROFILE_UNKNOWN)
        ]


def test_the_daily_sweep_leaves_unflagged_firms_untouched() -> None:
    flagged = cycle_setup("sweep-on")
    dark = cycle_setup("sweep-off")
    for setup in (flagged, dark):
        with tenant_context(setup.tenant.pk):
            _cycle(setup)
            Engagement.objects.filter(pk=setup.engagement.pk).update(responsible=None)
    dark.tenant.features = []
    dark.tenant.save(update_fields=["features"])

    totals = open_deterministic_exceptions()

    assert totals == {"evaluated": 1, "skipped_no_owner": 0}
    with tenant_context(flagged.tenant.pk):
        assert CycleException.objects.count() == 1
    with tenant_context(dark.tenant.pk):
        assert CycleException.objects.count() == 0


def test_two_racing_sweeps_open_one_exception() -> None:
    setup = cycle_setup()
    with tenant_context(setup.tenant.pk):
        _cycle(setup)
        Engagement.objects.filter(pk=setup.engagement.pk).update(responsible=None)
    barrier = threading.Barrier(2, timeout=TIMEOUT)
    errors: list[BaseException] = []

    def sweep() -> None:
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"SET ROLE {RUNTIME_ROLE}")
            with tenant_context(setup.tenant.pk):
                barrier.wait()
                evaluate_tenant_exceptions(setup.tenant, today=TODAY)
        except BaseException as error:  # noqa: BLE001 - re-raised on the test thread
            errors.append(error)
        finally:
            connections.close_all()

    workers = [threading.Thread(target=sweep, daemon=True) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(TIMEOUT)
    assert not any(worker.is_alive() for worker in workers)
    assert errors == []
    with tenant_context(setup.tenant.pk):
        assert CycleException.objects.count() == 1
        assert CycleEvent.objects.filter(type="exception.opened").count() == 1


@pytest.mark.parametrize("code", FORBIDDEN_WAIVERS)
def test_forbidden_waivers_are_refused_without_writes(code: str) -> None:
    setup = cycle_setup()
    with tenant_context(setup.tenant.pk):
        cycle = _cycle(setup)
        problem = _open(setup, cycle, code, uuid4())
        problem = transition_exception(
            setup.actor,
            problem.pk,
            ExceptionState.HANDLING,
            expected_version=problem.version,
        )
        before = _writes()
        with pytest.raises(IllegalTransition, match="aceita como risco"):
            transition_exception(
                setup.actor,
                problem.pk,
                ExceptionState.RISK_ACCEPTED,
                expected_version=problem.version,
                note="Risco assumido pelo escritório.",
            )
        assert _writes() == before


def test_every_registered_reason_outside_the_allow_list_is_unwaivable() -> None:
    registered = set(exceptions._REASONS)
    assert registered >= WAIVABLE_REASONS
    assert set(FORBIDDEN_WAIVERS) <= registered - WAIVABLE_REASONS
    for code in registered - WAIVABLE_REASONS:
        assert not exceptions.is_waivable(code), code


def test_a_policy_reason_is_waived_only_with_a_note() -> None:
    setup = cycle_setup()
    with tenant_context(setup.tenant.pk):
        cycle = _cycle(setup)
        problem = _open(setup, cycle, REQUEST_OVERDUE, uuid4())
        with pytest.raises(IllegalTransition):
            transition_exception(
                setup.actor,
                problem.pk,
                ExceptionState.RESOLVED,
                expected_version=problem.version,
                note="Pular a triagem não é permitido.",
            )
        problem = transition_exception(
            setup.actor,
            problem.pk,
            ExceptionState.HANDLING,
            expected_version=problem.version,
        )
        with pytest.raises(IllegalTransition):
            transition_exception(
                setup.actor,
                problem.pk,
                ExceptionState.RISK_ACCEPTED,
                expected_version=problem.version,
            )
        waived = transition_exception(
            setup.actor,
            problem.pk,
            ExceptionState.RISK_ACCEPTED,
            expected_version=problem.version,
            note="Cliente regulariza no próximo ciclo.",
        )
        assert waived.state == ExceptionState.RISK_ACCEPTED
        event = CycleEvent.objects.get(
            type="exception.transitioned", payload__to=waived.state
        )
        note = InternalNote.objects.get(pk=event.payload["note_id"])
        assert note.body == "Cliente regulariza no próximo ciclo."
        assert "Cliente" not in str(event.payload)


def test_commands_are_versioned_and_two_stale_callers_cannot_both_win() -> None:
    setup = cycle_setup()
    with tenant_context(setup.tenant.pk):
        cycle = _cycle(setup)
        problem = _open(setup, cycle, REQUEST_OVERDUE, uuid4())
        rendered = problem.version
        transition_exception(
            setup.actor, problem.pk, ExceptionState.HANDLING, expected_version=rendered
        )
        before = _writes()
        # The second caller rendered the same revision: refused, nothing written.
        with pytest.raises(VersionConflict):
            transition_exception(
                setup.actor,
                problem.pk,
                ExceptionState.HANDLING,
                expected_version=rendered,
            )
        with pytest.raises(VersionConflict):
            assign_exception(
                setup.actor, problem.pk, setup.actor.pk, expected_version=rendered
            )
        assert _writes() == before


def test_an_unassigned_staff_accountant_is_denied_and_owners_must_see_the_client() -> (
    None
):
    setup = cycle_setup()
    with tenant_context(setup.tenant.pk):
        cycle = _cycle(setup)
        problem = _open(setup, cycle, REQUEST_OVERDUE, uuid4())
        before = _writes()
        with pytest.raises(Http404):
            assign_exception(
                setup.staff, problem.pk, setup.staff.pk, expected_version=1
            )
        with pytest.raises(Http404):
            transition_exception(
                setup.staff, problem.pk, ExceptionState.HANDLING, expected_version=1
            )
        # The owner may not hand it to someone whose portfolio lacks the client.
        with pytest.raises(ValidationError):
            assign_exception(
                setup.actor, problem.pk, setup.staff.pk, expected_version=1
            )
        assert _writes() == before

        ClientAssignment.objects.create(
            tenant=setup.tenant, client=setup.client, user=setup.staff
        )
        moved = assign_exception(
            setup.actor, problem.pk, setup.staff.pk, expected_version=1
        )
        assert (moved.owner_id, moved.version) == (setup.staff.pk, 2)
        assert (
            CycleEvent.objects.filter(
                type="exception.assigned", aggregate_id=problem.pk
            ).count()
            == 1
        )
        triaged = transition_exception(
            setup.staff, problem.pk, ExceptionState.HANDLING, expected_version=2
        )
        assert triaged.state == ExceptionState.HANDLING


def test_assignment_revoked_after_scope_lookup_prevents_an_exception_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = cycle_setup()
    with tenant_context(setup.tenant.pk):
        cycle = _cycle(setup)
        problem = _open(setup, cycle, REQUEST_OVERDUE, uuid4())
        assignment = ClientAssignment.objects.create(
            tenant=setup.tenant, client=setup.client, user=setup.staff
        )
        transition_exception(
            setup.staff, problem.pk, ExceptionState.HANDLING, expected_version=1
        )
        before = _writes()
    real_load = load_client

    def revoke() -> None:
        remove_assignment(
            actor=setup.actor,
            tenant_id=setup.tenant.pk,
            client_id=setup.client.pk,
            assignment_id=assignment.pk,
            expected_state=assignment_state(setup.client),
        )
        assert not ClientAssignment.objects.filter(pk=assignment.pk).exists()

    def after_scope(
        actor: User,
        tenant_id: UUID,
        client_id: UUID,
        *,
        lock: bool = False,
    ) -> ClientCompany:
        client = real_load(actor, tenant_id, client_id, lock=lock)
        if actor.pk == setup.staff.pk:
            run_in_thread_as_runtime(setup.tenant.pk, revoke)
        return client

    monkeypatch.setattr(cycle_services, "load_client", after_scope)
    with tenant_context(setup.tenant.pk):
        with pytest.raises(Http404):
            transition_exception(
                setup.staff,
                problem.pk,
                ExceptionState.RESOLVED,
                expected_version=2,
                note="Conferência concluída.",
            )
        assert _writes() == before


def test_the_portal_sees_only_client_visible_rows_with_a_vacuity_control() -> None:
    world = portal_setup("t14-portal")
    setup = world.graph
    with tenant_context(setup.tenant.pk):
        visible = _open(setup, world.cycle, CERTIFICATE_EXPIRING, setup.client.pk)
        hidden = _open(setup, world.cycle, TAX_PROFILE_UNKNOWN, world.cycle.pk)
        foreign_cycle = ServiceCycle.objects.get(client=world.other_client)
        foreign = CycleException.objects.create(
            tenant=setup.tenant,
            client=world.other_client,
            cycle=foreign_cycle,
            reason_code=CERTIFICATE_EXPIRING,
            subject_ref=world.other_client.pk,
            owner=setup.actor,
            review_on=PERIOD,
            client_visible=True,
            client_message="FOREIGN-CLIENT-MESSAGE",
        )
        ExceptionInternal.objects.create(
            tenant=setup.tenant,
            client=world.other_client,
            exception=foreign,
            next_action="FOREIGN-NEXT-ACTION",
        )
        internal_actions = list(
            ExceptionInternal.objects.filter(
                exception_id__in=[visible.pk, hidden.pk]
            ).values_list("next_action", flat=True)
        )
        # Vacuity control: the firm's own role reads all three rows.
        assert CycleException.objects.count() == 3
    with (
        tenant_context(setup.tenant.pk),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute("SET LOCAL ROLE app_portal")
        cursor.execute(
            "SELECT set_config('app.client_id', %s, true)", [str(setup.client.pk)]
        )
        cursor.execute("SELECT id, client_message FROM cycles_exception")
        assert cursor.fetchall() == [(visible.pk, visible.client_message)]
        cursor.execute(
            "SELECT set_config('app.client_id', %s, true)",
            [str(world.other_client.pk)],
        )
        cursor.execute("SELECT id FROM cycles_exception")
        assert cursor.fetchall() == [(foreign.pk,)]
        for statement in (
            "UPDATE cycles_exception SET state = 'resolvida'",
            "DELETE FROM cycles_exception",
        ):
            with pytest.raises(DatabaseError), transaction.atomic():
                cursor.execute(statement)

    portal = world.portal()
    for path in ("/", f"/ciclos/{world.cycle.pk}/"):
        body = portal.get(path).content.decode()
        assert "Precisamos da sua ajuda" in body, path
        assert visible.client_message in body, path
        for internal in (
            *internal_actions,
            REASONS[TAX_PROFILE_UNKNOWN].label,
            "FOREIGN-CLIENT-MESSAGE",
        ):
            assert internal not in body, (path, internal)


def test_a_provider_rejected_portal_invite_is_withdrawn_and_owned(
    settings: SettingsWrapper,
) -> None:
    settings.DELIVERY_PAUSED = False
    setup = cycle_setup("invite-reject")
    setup.tenant.features = ["cycles_r1", "import_r1"]
    setup.tenant.save(update_fields=["features"])
    acknowledge(setup, ("portal_invite",))
    with tenant_context(setup.tenant.pk):
        cycle = _cycle(setup)
        contact = ClientContact.objects.create(
            tenant=setup.tenant,
            client=setup.client,
            name="Dona Maria",
            email="maria@padaria.test",
        )
    browser = _firm(setup)
    queued = browser.post(
        reverse("client-contact-invite", args=[setup.client.pk, contact.pk]),
        {"convite-role": TenantRole.CLIENT_OWNER, "convite-version": contact.version},
    )
    assert queued.status_code == HTTPStatus.FOUND
    with FakeSMTP(mode="reject") as server:
        settings.EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
        settings.EMAIL_HOST = "127.0.0.1"
        settings.EMAIL_PORT = server.port
        settings.EMAIL_HOST_USER = ""
        settings.EMAIL_HOST_PASSWORD = ""
        settings.EMAIL_USE_TLS = False
        settings.EMAIL_USE_SSL = False
        settings.EMAIL_TIMEOUT = 3
        dispatch_outbox()

    invite = Invite.objects.get(tenant=setup.tenant, email="maria@padaria.test")
    assert invite.revoked_at is not None
    assert PlatformEvent.objects.filter(
        action=AuditAction.INVITE_REVOKED,
        tenant_id=setup.tenant.pk,
        metadata__invite_id=str(invite.pk),
        metadata__system=True,
    ).exists()
    with tenant_context(setup.tenant.pk):
        message = OutboundMessage.objects.get(kind="portal_invite")
        assert message.state == MessageState.REJECTED
        problem = CycleException.objects.get(cycle=cycle)
        assert (problem.reason_code, problem.subject_ref, problem.state) == (
            INVITE_REJECTED,
            invite.pk,
            ExceptionState.OPEN,
        )
        assert problem.owner_id == setup.actor.pk
        assert ExceptionInternal.objects.get(exception=problem).evidence_refs == [
            str(message.pk)
        ]


def test_the_cycle_page_and_workspace_render_drawers_and_post_versioned_actions() -> (
    None
):
    setup = cycle_setup()
    with tenant_context(setup.tenant.pk):
        cycle = _cycle(setup)
        problem = _open(setup, cycle, "delivery_unknown", uuid4())
    browser = _firm(setup)
    page = browser.get(reverse("cycle-detail", args=[cycle.pk])).content.decode()
    assert 'id="pendencias"' in page
    assert 'data-reason="delivery_unknown"' in page
    assert "Pendência: resultado incerto do envio." in page
    workspace = browser.get(
        f"{reverse('client-detail', args=[setup.client.pk])}?periodo=2026-09"
    ).content.decode()
    assert 'data-reason="delivery_unknown"' in workspace

    url = reverse("cycle-exception-action", args=[problem.pk])
    triaged = browser.post(url, {"action": "em_tratamento", "expected_version": 1})
    assert triaged.status_code == HTTPStatus.FOUND
    assert triaged["Location"].endswith("#pendencias")
    stale = browser.post(url, {"action": "em_tratamento", "expected_version": 1})
    assert stale.status_code == HTTPStatus.CONFLICT
    handling = browser.get(reverse("cycle-detail", args=[cycle.pk])).content.decode()
    assert 'value="aceita_como_risco"' not in handling
    assert "não pode ser aceita como risco" in handling
    refused = browser.post(
        url,
        {"action": "aceita_como_risco", "expected_version": 2, "note": "Risco."},
    )
    assert refused.status_code == HTTPStatus.UNPROCESSABLE_CONTENT
    resolved = browser.post(
        url,
        {
            "action": "resolvida",
            "expected_version": 2,
            "note": "Servidor confirmou o envio.",
            "origin": "cliente",
        },
    )
    assert resolved.status_code == HTTPStatus.FOUND
    assert resolved["Location"] == (
        f"/clientes/{setup.client.pk}/?periodo=2026-09"
        f"&pendencia={problem.pk}#pendencias"
    )
    with tenant_context(setup.tenant.pk):
        assert CycleException.objects.get(pk=problem.pk).state == (
            ExceptionState.RESOLVED
        )
