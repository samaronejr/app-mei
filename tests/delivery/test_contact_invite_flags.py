"""T48 contact-origin invitations must not escape the durable delivery boundary."""

import json
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest
from allauth.account.models import EmailAddress
from django.core import mail
from django.core.management import call_command
from django.db import connection
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.invites import (
    InviteScope,
    accept_invite,
    register_and_accept,
    resolve_invite,
)
from apps.accounts.models import User
from apps.clients import views_onboarding
from apps.clients.models import ClientContact
from apps.core.tenancy import tenant_context
from apps.cycles.effects.executors import execute
from apps.cycles.services.approvals import approve
from apps.cycles.services.requests import RequestFields, create_request
from apps.delivery import journal, outbox
from apps.delivery.gateway import send
from apps.delivery.models import (
    AutomationPause,
    DeliveryEffect,
    MessageState,
    OutboundMessage,
)
from apps.delivery.notice_authority import configure, validate_notice
from apps.delivery.services import claim_batch, set_pause
from apps.delivery.tasks import dispatch_outbox
from apps.tenants.models import Invite, Membership, TenantRole
from tests.cycles.approval_fixtures import ApprovalGraph, approval_graph
from tests.cycles.publication_fixtures import PublicationGraph, publication_graph
from tests.delivery.conftest import DeliverySetup
from tests.delivery.smtp import FakeSMTP
from tests.delivery.test_portal_invite_outbox import _tokens
from tests.ui.factories import PASSWORD, Firm, make_firm

pytestmark = pytest.mark.django_db(transaction=True)

ADDRESS = "contact@t48.example.test"


@pytest.fixture
def contact_origin(
    settings: SettingsWrapper, tmp_path: Path
) -> tuple[Firm, ClientContact, Client]:
    settings.DELIVERY_PAUSED = False
    settings.MEDIA_ROOT = tmp_path
    firm = make_firm("t48-contact", client_count=1)
    firm.tenant.features = ["import_r1", "cycles_r1"]
    firm.tenant.save(update_fields=["features"])
    with tenant_context(firm.tenant.pk):
        contact = ClientContact.objects.create(
            tenant=firm.tenant, client=firm.clients[0], name="Contato", email=ADDRESS
        )
        configure(
            firm.owner,
            kinds=("contact_portal_invite",),
            acknowledged=True,
            expected_version=0,
        )
    return firm, contact, firm.as_owner()


def _flags(firm: Firm, features: list[str]) -> None:
    firm.tenant.features = features
    firm.tenant.save(update_fields=["features"])


def _post(web: Client, contact: ClientContact) -> int:
    return web.post(
        reverse("client-contact-invite", args=[contact.client_id, contact.pk]),
        {"convite-role": TenantRole.CLIENT_OWNER, "convite-version": contact.version},
    ).status_code


def _rows(firm: Firm) -> list[OutboundMessage]:
    with tenant_context(firm.tenant.pk):
        return list(OutboundMessage.objects.order_by("pk"))


@pytest.mark.parametrize("features", [["import_r1"], ["import_r1", "cycles_r1"]])
def test_setup_row_carries_the_invitation_delivery_state(
    contact_origin: tuple[Firm, ClientContact, Client], features: list[str]
) -> None:
    # Given a queued contact invitation, with or without a Comunicação queue.
    firm, contact, web = contact_origin
    _flags(firm, features)
    assert _post(web, contact) == 302
    [message] = _rows(firm)

    # When the owner reopens the client's setup page.
    page = web.get(reverse("client-setup", args=[contact.client_id]))

    # Then the contact row carries the durable message's own state.
    [row] = [item for item in page.context["contacts"] if item["row"].pk == contact.pk]
    assert row["pending_invite_id"] == message.invite_id
    assert row["pending_delivery"] == MessageState.QUEUED.label


@pytest.mark.parametrize("features", [["import_r1"], ["import_r1", "cycles_r1"]])
def test_contact_submission_queues_without_synchronous_mail(
    contact_origin: tuple[Firm, ClientContact, Client], features: list[str]
) -> None:
    # Given the real import-gated contact route, with or without cycle entitlement.
    firm, contact, web = contact_origin
    _flags(firm, features)
    mail.outbox.clear()

    # When the contact invitation is submitted.
    assert _post(web, contact) == 302

    # Then only a durable, recipient-bound invitation intent exists.
    assert mail.outbox == []
    [message] = _rows(firm)
    assert message.state == MessageState.QUEUED
    assert message.recipients_json == [ADDRESS]
    assert message.contact_id == contact.pk
    assert message.invite_id is not None
    assert message.kind == "contact_portal_invite"
    assert message.notice_authorization_bytes is not None
    binding = json.loads(bytes(message.notice_authorization_bytes))
    assert binding["feature"] == "import_r1"
    assert binding["kind"] == "contact_portal_invite"
    assert binding["basis"]["type"] == "tenant_policy"
    with tenant_context(firm.tenant.pk):
        assert validate_notice(message) == (True, "authorized")
    # PLATFORM_QUERY_OK: exact invitation referenced by the durable intent.
    assert Invite.objects.get(pk=message.invite_id).contact_id == contact.pk


@pytest.mark.parametrize("features", [[], ["cycles_r1"]])
def test_contact_route_is_absent_without_import_entitlement(
    contact_origin: tuple[Firm, ClientContact, Client], features: list[str]
) -> None:
    # Given no import entitlement, even when cycles is enabled.
    firm, contact, web = contact_origin
    _flags(firm, features)
    mail.outbox.clear()
    # When an old contact URL is submitted.
    assert _post(web, contact) == 404

    # Then neither an invitation nor an external effect is created.
    assert _rows(firm) == []
    assert mail.outbox == []
    # PLATFORM_QUERY_OK: only the test firm's invitation root.
    assert not Invite.objects.filter(tenant=firm.tenant).exists()


def test_import_kill_hides_contact_route_even_with_cycles_enabled(
    contact_origin: tuple[Firm, ClientContact, Client],
) -> None:
    # Given both entitlements, but the import surface globally disabled.
    firm, contact, web = contact_origin
    mail.outbox.clear()

    # When the real route is submitted under the kill switch.
    with override_settings(AXORIM_DISABLED_FEATURES=["import_r1"]):
        assert _post(web, contact) == 404

    # Then cycles cannot rescue this import-origin operation.
    assert _rows(firm) == []
    assert mail.outbox == []
    # PLATFORM_QUERY_OK: only the test firm's invitation root.
    assert not Invite.objects.filter(tenant=firm.tenant).exists()


def test_cycles_kill_does_not_force_import_contact_to_legacy_mail(
    contact_origin: tuple[Firm, ClientContact, Client],
) -> None:
    # Given both flags but a global cycle kill, with import still authorized.
    firm, contact, web = contact_origin
    mail.outbox.clear()

    # When the import contact invitation is submitted.
    with override_settings(AXORIM_DISABLED_FEATURES=["cycles_r1"]):
        assert _post(web, contact) == 302

    # Then the independently authorized import origin still uses durable delivery.
    assert mail.outbox == []
    assert _rows(firm)[0].state == MessageState.QUEUED


@pytest.mark.parametrize("features", [[], ["cycles_r1"]])
def test_original_detail_endpoint_keeps_its_own_flag_contract(
    contact_origin: tuple[Firm, ClientContact, Client], features: list[str]
) -> None:
    # Given the original endpoint, independent of the new import-only contact door.
    firm, contact, web = contact_origin
    _flags(firm, features)
    mail.outbox.clear()
    if features:
        with tenant_context(firm.tenant.pk):
            configure(
                firm.owner,
                kinds=("portal_invite",),
                acknowledged=True,
                expected_version=1,
            )

    # When the legacy client-detail invitation is issued.
    response = web.post(
        reverse("portal-invite-issue", args=[contact.client_id]),
        {"email": ADDRESS, "role": TenantRole.CLIENT_OWNER},
    )

    # Then both-off retains legacy mail, and cycles-only retains its outbox kind.
    assert response.status_code == 302
    if features:
        [message] = _rows(firm)
        assert message.kind == "portal_invite"
        assert message.state == MessageState.QUEUED
        assert mail.outbox == []
    else:
        assert _rows(firm) == []
        assert [message.to for message in mail.outbox] == [[ADDRESS]]


def test_contact_intent_dispatches_after_cycles_entitlement_is_removed(
    contact_origin: tuple[Firm, ClientContact, Client],
    smtp_server: Callable[[str], FakeSMTP],
) -> None:
    # Given a real contact-origin intent queued with both flags, then import only.
    firm, contact, web = contact_origin
    assert _post(web, contact) == 302
    [message] = _rows(firm)
    _flags(firm, ["import_r1"])
    server = smtp_server("accept")
    boundaries: list[bool] = []

    # When the actual platform dispatcher runs and its egress boundary is observed.
    def observe_boundary() -> None:
        boundaries.append(connection.in_atomic_block)

    from apps.delivery.tasks import dispatch_tenant  # noqa: PLC0415

    assert dispatch_tenant(str(firm.tenant.pk), before_network_io=observe_boundary) == 1
    assert dispatch_outbox() == 0

    # Then SMTP accepted once outside a DB transaction, with durable receipt/journal.
    [stored] = _rows(firm)
    assert stored.state == MessageState.ACCEPTED
    assert len(server.received) == 1
    assert boundaries == [False]
    records = journal.read(firm.tenant.pk)
    assert {record.outcome for record in records} == {
        "intent_started",
        MessageState.ACCEPTED,
    }
    assert {record.intent for record in records} == {message.business_intent_key}
    with tenant_context(firm.tenant.pk):
        assert DeliveryEffect.objects.filter(message=message).count() == 1


def test_platform_dispatch_visits_import_only_contact_intents(
    contact_origin: tuple[Firm, ClientContact, Client],
    smtp_server: Callable[[str], FakeSMTP],
) -> None:
    # Given a committed contact intent whose firm no longer has cycles enabled.
    firm, contact, web = contact_origin
    assert _post(web, contact) == 302
    _flags(firm, ["import_r1"])
    server = smtp_server("accept")

    # When beat calls the real platform dispatcher.
    assert dispatch_outbox() == 1

    # Then the import-only tenant is visited, not silently skipped.
    assert len(server.received) == 1
    assert _rows(firm)[0].state == MessageState.ACCEPTED


@pytest.mark.parametrize("pause", ["global", "tenant"])
def test_import_only_contact_intent_obeys_pause(
    contact_origin: tuple[Firm, ClientContact, Client],
    settings: SettingsWrapper,
    pause: str,
) -> None:
    # Given paused delivery before an import-only contact submission.
    firm, contact, web = contact_origin
    if pause == "global":
        settings.DELIVERY_PAUSED = True
    else:
        with tenant_context(firm.tenant.pk):
            set_pause(firm.owner, paused=True, expected_version=1)
    _flags(firm, ["import_r1"])
    mail.outbox.clear()

    # When the owner submits and the dispatcher runs.
    assert _post(web, contact) == 302
    assert dispatch_outbox() == 0

    # Then pause cannot be bypassed by synchronous legacy mail.
    assert mail.outbox == []
    assert _rows(firm)[0].state == MessageState.QUEUED
    assert journal.read(firm.tenant.pk) == []


def test_duplicate_contact_submit_keeps_one_durable_intent(
    contact_origin: tuple[Firm, ClientContact, Client],
) -> None:
    # Given a committed import-only invitation.
    firm, contact, web = contact_origin
    _flags(firm, ["import_r1"])
    mail.outbox.clear()
    assert _post(web, contact) == 302

    # When the exact same form is submitted again.
    assert _post(web, contact) == 409

    # Then the live invitation and its durable delivery intent remain singletons.
    assert len(_rows(firm)) == 1
    # PLATFORM_QUERY_OK: only this test firm's invitation root.
    assert Invite.objects.filter(tenant=firm.tenant).count() == 1
    assert mail.outbox == []


def test_import_only_enqueue_failure_rolls_back_invitation_and_intent(
    contact_origin: tuple[Firm, ClientContact, Client],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given the real enqueue seam, with a crash immediately after it writes.
    firm, contact, web = contact_origin
    _flags(firm, ["import_r1"])

    def crashing[**P](
        real: Callable[P, OutboundMessage],
    ) -> Callable[P, OutboundMessage]:
        def crash_after_enqueue(*args: P.args, **kwargs: P.kwargs) -> OutboundMessage:
            real(*args, **kwargs)
            msg = "T48 crash after durable insert"
            raise RuntimeError(msg)

        return crash_after_enqueue

    monkeypatch.setattr(
        views_onboarding,
        "enqueue_portal_invite",
        crashing(outbox.enqueue_portal_invite),
    )
    web.raise_request_exception = False
    mail.outbox.clear()

    # When the real request crashes between enqueue and commit.
    assert _post(web, contact) == 500

    # Then neither half of the intent commits and no post-commit mail escapes.
    assert _rows(firm) == []
    assert mail.outbox == []
    # PLATFORM_QUERY_OK: only this test firm's invitation root.
    assert not Invite.objects.filter(tenant=firm.tenant).exists()


@pytest.mark.parametrize("revocation", ["contact", "authority"])
def test_revocation_before_dispatch_prevents_contact_invitation_send(
    contact_origin: tuple[Firm, ClientContact, Client],
    smtp_server: Callable[[str], FakeSMTP],
    revocation: str,
) -> None:
    # Given a committed invitation, then a live contact or issuer revocation.
    firm, contact, web = contact_origin
    assert _post(web, contact) == 302
    if revocation == "contact":
        with tenant_context(firm.tenant.pk):
            ClientContact.objects.filter(pk=contact.pk).update(
                channel_email_allowed=False
            )
    else:
        # PLATFORM_QUERY_OK: only this firm's issuing actor membership.
        Membership.objects.filter(tenant=firm.tenant, user=firm.owner).update(
            is_active=False
        )
    server = smtp_server("accept")

    # When the real dispatcher re-evaluates the queued invitation.
    dispatch_outbox()

    # Then no DATA or journal start exists; the intent records the refusal.
    [message] = _rows(firm)
    assert message.state == MessageState.CANCELLED
    assert server.received == []
    assert journal.read(firm.tenant.pk) == []


@pytest.mark.parametrize("disabled", [["import_r1"], ["import_r1", "cycles_r1"]])
def test_import_kill_is_rechecked_after_contact_intent_claim(
    contact_origin: tuple[Firm, ClientContact, Client],
    smtp_server: Callable[[str], FakeSMTP],
    disabled: list[str],
) -> None:
    # Given a claimed contact-origin intent; cycles remains on in the first case.
    firm, contact, web = contact_origin
    assert _post(web, contact) == 302
    [claim] = claim_batch(firm.tenant.pk)
    server = smtp_server("accept")

    # When the global kill changes between claim and network authorization.
    with override_settings(AXORIM_DISABLED_FEATURES=disabled):
        reply = send(claim)

    # Then the import origin cannot fall back to cycles entitlement.
    assert reply.state == MessageState.CANCELLED
    assert server.received == []
    assert journal.read(firm.tenant.pk) == []


@pytest.mark.parametrize("kind", ["notice", "request_notice", "request_reminder"])
def test_import_entitlement_does_not_dispatch_other_kinds(
    delivery: DeliverySetup, smtp_server: Callable[[str], FakeSMTP], kind: str
) -> None:
    # Given genuine published-cycle sources and an immutable T53-bound envelope.
    with tenant_context(delivery.graph.tenant.pk):
        request = create_request(
            delivery.graph.actor,
            delivery.cycle.pk,
            RequestFields("Documento pendente"),
            expected_version=delivery.cycle.version,
        )
        delivery.graph.client.refresh_from_db()
        message = outbox.enqueue(
            delivery.graph.actor,
            delivery.graph.client.pk,
            delivery.contact.pk,
            intent=delivery.intent,
            expected_version=delivery.graph.client.version,
            kind=kind,
            cycle_id=delivery.cycle.pk,
            evidence_request=request if kind == "request_reminder" else None,
        )
    delivery.graph.tenant.features = ["import_r1"]
    delivery.graph.tenant.save(update_fields=["features"])
    server = smtp_server("accept")

    # When the dispatcher visits the import-enabled firm.
    assert dispatch_outbox() == 0

    # Then import cannot authorize another delivery kind or reserve a send.
    assert delivery.reload(message).state == MessageState.QUEUED
    assert server.received == []
    assert journal.read(delivery.graph.tenant.pk) == []
    with tenant_context(delivery.graph.tenant.pk):
        assert not AutomationPause.objects.filter(reserved_today__gt=0).exists()
        assert delivery.reload(message).attempts == 0


@pytest.mark.parametrize("publication", [False, True])
def test_import_entitlement_cannot_claim_approved_effects(
    settings: SettingsWrapper,
    tmp_path: Path,
    smtp_server: Callable[[str], FakeSMTP],
    publication: bool,
) -> None:
    # Given a genuinely approved and executed consequential effect, not a retyped row.
    settings.DELIVERY_PAUSED = False
    settings.MEDIA_ROOT = tmp_path
    setup: ApprovalGraph | PublicationGraph
    if publication:
        setup = publication_graph("t48-publication")
    else:
        setup = approval_graph("t48-email")
        with tenant_context(setup.graph.tenant.pk):
            approve(setup.graph.actor, setup.proposal.pk, expected_version=1)
    with tenant_context(setup.graph.tenant.pk):
        assert execute(
            setup.graph.actor, setup.proposal.pk, expected_version=2
        ).executed
        message = OutboundMessage.objects.get(proposal_id=setup.proposal.pk)
    setup.graph.tenant.features = ["import_r1"]
    setup.graph.tenant.save(update_fields=["features"])
    server = smtp_server("accept")

    assert claim_batch(setup.graph.tenant.pk) == []
    assert dispatch_outbox() == 0
    with tenant_context(setup.graph.tenant.pk):
        message.refresh_from_db()
        assert message.kind == (
            "approved_publication_notice" if publication else "approved_email"
        )
        assert message.state == MessageState.QUEUED
        assert message.attempts == 0
        assert not AutomationPause.objects.filter(reserved_today__gt=0).exists()
    assert server.received == []
    assert journal.read(setup.graph.tenant.pk) == []


def test_import_claim_filter_precedes_limit_and_quota_reservation(
    contact_origin: tuple[Firm, ClientContact, Client],
    settings: SettingsWrapper,
) -> None:
    firm, contact, web = contact_origin
    with tenant_context(firm.tenant.pk):
        configure(
            firm.owner,
            kinds=("portal_invite", "contact_portal_invite"),
            acknowledged=True,
            expected_version=1,
        )
    assert (
        web.post(
            reverse("portal-invite-issue", args=[contact.client_id]),
            {"email": "cycle@t48.example.test", "role": TenantRole.CLIENT_OWNER},
        ).status_code
        == 302
    )
    assert _post(web, contact) == 302
    with tenant_context(firm.tenant.pk):
        cycle_invite = OutboundMessage.objects.get(kind="portal_invite")
        OutboundMessage.objects.filter(pk=cycle_invite.pk).update(
            next_attempt_at=timezone.now() - timedelta(minutes=1)
        )
    _flags(firm, ["import_r1"])
    settings.DELIVERY_DAILY_CAP = 1

    [claim] = claim_batch(firm.tenant.pk)

    with tenant_context(firm.tenant.pk):
        assert OutboundMessage.objects.get(pk=claim.message_id).kind == (
            "contact_portal_invite"
        )
        cycle_invite.refresh_from_db()
        assert cycle_invite.state == MessageState.QUEUED
        assert cycle_invite.attempts == 0
        assert cycle_invite.lease_owner is None
        assert AutomationPause.objects.get().reserved_today == 1


@pytest.mark.parametrize("origin", ["contact", "detail"])
@pytest.mark.parametrize("stage", ["claim", "egress"])
@pytest.mark.parametrize("kill", ["tenant", "global"])
def test_origin_kill_cannot_fall_back_to_the_other_flag(
    contact_origin: tuple[Firm, ClientContact, Client],
    smtp_server: Callable[[str], FakeSMTP],
    origin: str,
    stage: str,
    kill: str,
) -> None:
    firm, contact, web = contact_origin
    feature = "import_r1" if origin == "contact" else "cycles_r1"
    if origin == "contact":
        assert _post(web, contact) == 302
    else:
        with tenant_context(firm.tenant.pk):
            configure(
                firm.owner,
                kinds=("portal_invite",),
                acknowledged=True,
                expected_version=1,
            )
        assert (
            web.post(
                reverse("portal-invite-issue", args=[contact.client_id]),
                {"email": ADDRESS, "role": TenantRole.CLIENT_OWNER},
            ).status_code
            == 302
        )
    claims = claim_batch(firm.tenant.pk) if stage == "egress" else []
    if kill == "tenant":
        _flags(firm, ["cycles_r1" if feature == "import_r1" else "import_r1"])
    server = smtp_server("accept")

    with override_settings(
        AXORIM_DISABLED_FEATURES=[feature] if kill == "global" else []
    ):
        if stage == "claim":
            assert claim_batch(firm.tenant.pk) == []
            with tenant_context(firm.tenant.pk):
                assert AutomationPause.objects.get().reserved_today == 0
            assert _rows(firm)[0].state == MessageState.QUEUED
        else:
            [claim] = claims
            assert send(claim).state == MessageState.CANCELLED
            assert _rows(firm)[0].send_started_at is None
    assert server.received == []
    assert journal.read(firm.tenant.pk) == []


def test_import_only_retry_and_crash_receipt_prevent_duplicate_effect(
    contact_origin: tuple[Firm, ClientContact, Client],
    smtp_server: Callable[[str], FakeSMTP],
) -> None:
    # Given a contact-origin intent, now import-only, and a known SMTP 451 refusal.
    firm, contact, web = contact_origin
    assert _post(web, contact) == 302
    _flags(firm, ["import_r1"])
    down = smtp_server("temporary")
    assert dispatch_outbox() == 1
    [message] = _rows(firm)
    assert message.state == MessageState.TEMPORARY
    assert len(down.received) == 1
    [first_token] = _tokens(down)
    with tenant_context(firm.tenant.pk):
        OutboundMessage.objects.filter(pk=message.pk).update(
            next_attempt_at=timezone.now()
        )
    up = smtp_server("accept")
    [claim] = claim_batch(firm.tenant.pk)

    # When SMTP accepts but the process loses DB finalization after its journal reply.
    assert send(claim).state == MessageState.ACCEPTED
    [second_token] = _tokens(up)
    assert first_token != second_token
    portal = Client(SERVER_NAME=f"{firm.tenant.slug}-portal.localhost")
    assert portal.get(f"/convites/aceitar/{first_token}/").status_code == 410
    assert portal.get(f"/convites/aceitar/{second_token}/").status_code == 200
    with override_settings(DELIVERY_PAUSED=True):
        call_command("reconcile_effects")

    # Then journal recovery converges to acceptance and a later beat cannot resend.
    assert _rows(firm)[0].state == MessageState.ACCEPTED
    assert dispatch_outbox() == 0
    assert len(up.received) == 1
    records = journal.read(firm.tenant.pk)
    assert {record.outcome for record in records} == {
        "intent_started",
        MessageState.TEMPORARY,
        MessageState.ACCEPTED,
    }
    assert len({record.attempt_id for record in records}) == 2
    with tenant_context(firm.tenant.pk):
        assert DeliveryEffect.objects.filter(message=message).count() == 1


@pytest.mark.parametrize("change", ["revoke", "reacknowledge", "other_kind"])
def test_policy_change_after_import_claim_refuses_without_data(
    contact_origin: tuple[Firm, ClientContact, Client],
    smtp_server: Callable[[str], FakeSMTP],
    change: str,
) -> None:
    firm, contact, web = contact_origin
    _flags(firm, ["import_r1"])
    assert _post(web, contact) == 302
    [claim] = claim_batch(firm.tenant.pk)
    raw = _rows(firm)[0].notice_authorization_bytes
    with tenant_context(firm.tenant.pk):
        configure(
            firm.owner,
            kinds=()
            if change == "revoke"
            else ("portal_invite",)
            if change == "other_kind"
            else ("contact_portal_invite",),
            acknowledged=change != "revoke",
            expected_version=1,
        )
    server = smtp_server("accept")

    assert send(claim).state == MessageState.CANCELLED

    [stored] = _rows(firm)
    assert stored.notice_authorization_bytes == raw
    assert stored.send_started_at is None
    assert server.received == []
    assert journal.read(firm.tenant.pk) == []


@pytest.mark.parametrize("pause", ["global", "tenant"])
def test_pause_after_import_claim_prevents_egress(
    contact_origin: tuple[Firm, ClientContact, Client],
    smtp_server: Callable[[str], FakeSMTP],
    settings: SettingsWrapper,
    pause: str,
) -> None:
    firm, contact, web = contact_origin
    _flags(firm, ["import_r1"])
    assert _post(web, contact) == 302
    [claim] = claim_batch(firm.tenant.pk)
    if pause == "global":
        settings.DELIVERY_PAUSED = True
    else:
        with tenant_context(firm.tenant.pk):
            set_pause(firm.owner, paused=True, expected_version=1)
    server = smtp_server("accept")

    assert send(claim).state == MessageState.CANCELLED

    assert _rows(firm)[0].send_started_at is None
    assert server.received == []
    assert journal.read(firm.tenant.pk) == []


@pytest.mark.parametrize("existing", [False, True])
def test_import_only_dispatch_binds_acceptance_to_the_originating_contact(
    contact_origin: tuple[Firm, ClientContact, Client],
    smtp_server: Callable[[str], FakeSMTP],
    existing: bool,
) -> None:
    firm, contact, web = contact_origin
    _flags(firm, ["import_r1"])
    user = None
    if existing:
        user = User.objects.create_user(email=ADDRESS, password=PASSWORD)
        EmailAddress.objects.create(
            user=user, email=ADDRESS, verified=True, primary=True
        )
    assert _post(web, contact) == 302
    server = smtp_server("accept")
    assert dispatch_outbox() == 1
    [token] = _tokens(server)
    invite = resolve_invite(token)

    member = (
        accept_invite(invite=invite, user=user, expected_scope=InviteScope.CLIENT)
        if user is not None
        else register_and_accept(
            invite=invite, password=PASSWORD, expected_scope=InviteScope.CLIENT
        )[1]
    )

    assert member.is_active
    assert member.client_id == contact.client_id
    assert invite.contact_id == contact.pk
    with tenant_context(firm.tenant.pk):
        contact.refresh_from_db()
        assert contact.membership_id == member.pk
        assert DeliveryEffect.objects.count() == 1
    assert (
        Client(SERVER_NAME=f"{firm.tenant.slug}-portal.localhost")
        .get(f"/convites/aceitar/{token}/")
        .status_code
        == 410
    )


def test_import_only_unknown_smtp_outcome_never_retries(
    contact_origin: tuple[Firm, ClientContact, Client],
    smtp_server: Callable[[str], FakeSMTP],
) -> None:
    firm, contact, web = contact_origin
    _flags(firm, ["import_r1"])
    assert _post(web, contact) == 302
    server = smtp_server("disconnect")

    assert dispatch_outbox() == 1

    assert _rows(firm)[0].state == MessageState.UNKNOWN
    assert dispatch_outbox() == 0
    assert len(server.received) == 1
    with tenant_context(firm.tenant.pk):
        assert DeliveryEffect.objects.get().outcome == MessageState.UNKNOWN
