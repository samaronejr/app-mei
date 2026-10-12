"""In-product onboarding (AXR1-007): create, edit, engagement, tax profile, contacts,
assignments and portal invites — the services and the screens.

The review contract for every firm-side write is pinned here: the client comes
through the actor's portfolio (an unassigned staff accountant gets 404 and nothing is
written), a stale version from a second caller conflicts with nothing written, and
every change leaves an audit event.
"""

from datetime import date, timedelta
from http import HTTPStatus
from typing import Final

import pytest
from django.core import mail
from django.http import Http404
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditAction, Event
from apps.clients.maintenance import (
    ClientData,
    ConflictError,
    ContactData,
    DuplicateClientError,
    EngagementData,
    RetroNoteRequiredError,
    assignment_state,
    create_client,
    save_contact,
    save_engagement,
    set_tax_profile,
    update_client,
)
from apps.clients.models import (
    ClientAssignment,
    ClientCompany,
    ClientContact,
    Engagement,
    ProfileConfidence,
    ServiceKind,
    TaxProfile,
    TaxRegime,
)
from apps.core.tenancy import tenant_context
from apps.delivery.models import OutboundMessage
from apps.delivery.notice_authority import configure
from apps.tenants.models import Invite, TenantRole
from tests.isolation.rolecheck import assert_isolated_role
from tests.ui.factories import Firm, assign, client_base, make_cnpj, make_firm

pytestmark = pytest.mark.django_db(transaction=True)

FLAG: Final = "import_r1"


def _firm(slug: str, *, flagged: bool = True, clients: int = 1) -> Firm:
    firm = make_firm(slug, client_count=clients)
    if flagged:
        firm.tenant.features = [FLAG]
        firm.tenant.save(update_fields=["features"])
    return firm


def _events(firm: Firm, action: str) -> int:
    with tenant_context(firm.tenant.id):
        return Event.objects.filter(action=action).count()


# --- services -------------------------------------------------------------------


def test_create_refuses_a_cnpj_already_in_the_firm_but_not_one_in_another() -> None:
    assert_isolated_role()
    alpha, beta = _firm("alfa"), _firm("beta", clients=0)
    taken = alpha.clients[0].cnpj
    with tenant_context(alpha.tenant.id), pytest.raises(DuplicateClientError):
        create_client(
            actor=alpha.owner,
            tenant_id=alpha.tenant.id,
            data=ClientData(legal_name="Outro", cnpj=taken),
        )
    with tenant_context(beta.tenant.id):
        created = create_client(
            actor=beta.owner,
            tenant_id=beta.tenant.id,
            data=ClientData(legal_name="Mesmo CNPJ em outro escritório", cnpj=taken),
        )
    assert created.cnpj == taken
    assert _events(beta, AuditAction.CLIENT_CREATED) == 1


def test_a_staff_accountant_creating_a_client_is_assigned_to_it() -> None:
    firm = _firm("gama", clients=0)
    with tenant_context(firm.tenant.id):
        client = create_client(
            actor=firm.accountant,
            tenant_id=firm.tenant.id,
            data=ClientData(
                legal_name="Nova MEI",
                cnpj=make_cnpj(client_base(1, "gama")),
            ),
        )
        assigned = ClientAssignment.objects.filter(
            client=client,
            user=firm.accountant,
        ).exists()
    assert assigned


def test_two_editors_holding_the_same_version_cannot_both_save() -> None:
    firm = _firm("delta")
    client = firm.clients[0]
    with tenant_context(firm.tenant.id):
        update_client(
            actor=firm.owner,
            tenant_id=firm.tenant.id,
            client_id=client.pk,
            expected_version=1,
            changes={"trade_name": "Primeira"},
        )
        with pytest.raises(ConflictError):
            update_client(
                actor=firm.owner,
                tenant_id=firm.tenant.id,
                client_id=client.pk,
                expected_version=1,
                changes={"trade_name": "Segunda"},
            )
        stored = ClientCompany.objects.get(pk=client.pk)
    assert (stored.trade_name, stored.version) == ("Primeira", 2)
    assert _events(firm, AuditAction.CLIENT_UPDATED) == 1


def test_an_unassigned_staff_accountant_gets_404_and_writes_nothing() -> None:
    assert_isolated_role()
    firm = _firm("epsilon")
    client = firm.clients[0]
    with tenant_context(firm.tenant.id):
        with pytest.raises(Http404):
            update_client(
                actor=firm.accountant,
                tenant_id=firm.tenant.id,
                client_id=client.pk,
                expected_version=1,
                changes={"trade_name": "Invasor"},
            )
        with pytest.raises(Http404):
            save_contact(
                actor=firm.accountant,
                tenant_id=firm.tenant.id,
                client_id=client.pk,
                data=ContactData(name="X"),
            )
        with pytest.raises(Http404):
            save_engagement(
                actor=firm.accountant,
                tenant_id=firm.tenant.id,
                client_id=client.pk,
                data=EngagementData(ServiceKind.MEI_MENSAL, date(2026, 1, 1)),
            )
        assert ClientContact.objects.count() == 0
        # Only the backfill-free fixture state: no engagement was written.
        assert Engagement.objects.filter(client=client).count() == 0
        assert ClientCompany.objects.get(pk=client.pk).version == 1
    assert _events(firm, AuditAction.CLIENT_UPDATED) == 0


def test_contact_and_engagement_edits_carry_their_versions() -> None:
    firm = _firm("zeta")
    client = firm.clients[0]
    with tenant_context(firm.tenant.id):
        contact = save_contact(
            actor=firm.owner,
            tenant_id=firm.tenant.id,
            client_id=client.pk,
            data=ContactData(name="Ana", email="ana@cliente.test"),
        )
        engagement = save_engagement(
            actor=firm.owner,
            tenant_id=firm.tenant.id,
            client_id=client.pk,
            data=EngagementData(ServiceKind.MEI_MENSAL, date(2026, 1, 1)),
        )
        # The first caller holding version 1 wins ...
        save_contact(
            actor=firm.owner,
            tenant_id=firm.tenant.id,
            client_id=client.pk,
            data=ContactData(name="Ana Souza", email="ana@cliente.test"),
            contact_id=contact.pk,
            expected_version=1,
        )
        save_engagement(
            actor=firm.owner,
            tenant_id=firm.tenant.id,
            client_id=client.pk,
            data=EngagementData(ServiceKind.OUTRO, date(2026, 1, 1)),
            engagement_id=engagement.pk,
            expected_version=1,
        )
        # ... and the second caller holding the same version is refused.
        with pytest.raises(ConflictError):
            save_contact(
                actor=firm.owner,
                tenant_id=firm.tenant.id,
                client_id=client.pk,
                data=ContactData(name="Perdida"),
                contact_id=contact.pk,
                expected_version=1,
            )
        with pytest.raises(ConflictError):
            save_engagement(
                actor=firm.owner,
                tenant_id=firm.tenant.id,
                client_id=client.pk,
                data=EngagementData(ServiceKind.DOCUMENTOS_MENSAL, date(2026, 1, 1)),
                engagement_id=engagement.pk,
                expected_version=1,
            )
        contact.refresh_from_db()
        engagement.refresh_from_db()
    assert (contact.name, contact.version) == ("Ana Souza", 2)
    assert (engagement.service_kind, engagement.version) == (ServiceKind.OUTRO, 2)


def test_a_new_regime_closes_the_previous_and_a_retro_edit_needs_a_note() -> None:
    firm = _firm("eta")
    client = firm.clients[0]
    today = timezone.localdate()
    with tenant_context(firm.tenant.id):
        # Filling unknown history is not a retro edit: no note needed.
        set_tax_profile(
            actor=firm.owner,
            tenant_id=firm.tenant.id,
            client_id=client.pk,
            regime=TaxRegime.MEI,
            mei_category="common",
            valid_from=date(2020, 1, 1),
            expected_revision=1,
        )
        # From today on: the MEI claim is closed at today, not rewritten.
        set_tax_profile(
            actor=firm.owner,
            tenant_id=firm.tenant.id,
            client_id=client.pk,
            regime=TaxRegime.SIMPLES,
            valid_from=today,
            expected_revision=2,
        )
        current = list(
            TaxProfile.objects.filter(client=client, superseded_at__isnull=True)
            .order_by("valid_from")
            .values_list("regime", "valid_from", "valid_to"),
        )
        assert current == [
            (TaxRegime.MEI, date(2020, 1, 1), today),
            (TaxRegime.SIMPLES, today, None),
        ]
        # Restating 2023 contradicts a recorded belief: a note is required.
        with pytest.raises(RetroNoteRequiredError):
            set_tax_profile(
                actor=firm.owner,
                tenant_id=firm.tenant.id,
                client_id=client.pk,
                regime=TaxRegime.SIMPLES,
                valid_from=date(2023, 1, 1),
                valid_to=date(2024, 1, 1),
                expected_revision=3,
            )
        assert ClientCompany.objects.get(pk=client.pk).tax_profile_revision == 3
        set_tax_profile(
            actor=firm.owner,
            tenant_id=firm.tenant.id,
            client_id=client.pk,
            regime=TaxRegime.SIMPLES,
            valid_from=date(2023, 1, 1),
            valid_to=date(2024, 1, 1),
            note="Desenquadrado em 2023 conforme termo recebido.",
            expected_revision=3,
        )
        # And a stale revision is a conflict, not a silent overwrite.
        with pytest.raises(ConflictError):
            set_tax_profile(
                actor=firm.owner,
                tenant_id=firm.tenant.id,
                client_id=client.pk,
                regime=TaxRegime.MEI,
                mei_category="common",
                valid_from=today + timedelta(days=30),
                expected_revision=3,
            )


# --- screens --------------------------------------------------------------------


def test_the_onboarding_screens_are_absent_while_the_flag_is_off() -> None:
    firm = _firm("teta", flagged=False)
    browser = firm.as_owner()
    client = firm.clients[0]
    for url in (
        reverse("client-create"),
        reverse("client-edit", args=[client.pk]),
        reverse("client-setup", args=[client.pk]),
    ):
        assert browser.get(url).status_code == HTTPStatus.NOT_FOUND
    listing = browser.get(reverse("clients-list")).content.decode()
    assert reverse("client-create") not in listing


def test_create_edit_and_set_up_a_client_through_the_screens() -> None:
    firm = _firm("iota", clients=0)
    browser = firm.as_owner()
    listing = browser.get(reverse("clients-list")).content.decode()
    assert reverse("client-create") in listing
    assert reverse("client-import") in listing

    bad = browser.post(reverse("client-create"), {"cnpj": "123", "legal_name": "X"})
    assert bad.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    cnpj = make_cnpj(client_base(3, "iota"))
    created = browser.post(
        reverse("client-create"),
        {"cnpj": cnpj[:2] + "." + cnpj[2:], "legal_name": "Padaria Iota MEI"},
    )
    assert created.status_code == HTTPStatus.FOUND
    with tenant_context(firm.tenant.id):
        client = ClientCompany.objects.get()
    assert client.cnpj == cnpj
    assert created["Location"] == reverse("client-setup", args=[client.pk])

    again = browser.post(
        reverse("client-create"),
        {"cnpj": cnpj, "legal_name": "Duplicada"},
    )
    assert again.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "já está cadastrado" in again.content.decode()

    setup = browser.get(reverse("client-setup", args=[client.pk]))
    assert setup.status_code == HTTPStatus.OK

    edit = {"legal_name": "Padaria Iota Ltda", "status": "active", "version": 1}
    assert browser.post(reverse("client-edit", args=[client.pk]), edit).status_code == (
        HTTPStatus.FOUND
    )
    stale = browser.post(reverse("client-edit", args=[client.pk]), edit)
    assert stale.status_code == HTTPStatus.CONFLICT

    response = browser.post(
        reverse("client-tax-profile", args=[client.pk]),
        {
            "tax-regime": "mei",
            "tax-mei_category": "common",
            "tax-valid_from": "2021-03-01",
            "tax-expected_revision": 1,
        },
    )
    assert response.status_code == HTTPStatus.FOUND
    response = browser.post(
        reverse("client-engagement-create", args=[client.pk]),
        {
            "eng-service_kind": "mei_mensal",
            "eng-starts_on": "2026-01-01",
            "eng-responsible": str(firm.accountant.pk),
        },
    )
    assert response.status_code == HTTPStatus.FOUND
    with tenant_context(firm.tenant.id):
        client.refresh_from_db()
        empty_state = assignment_state(client)
    add = {
        "resp-user": str(firm.accountant.pk),
        "resp-role": "primary",
        "resp-state": empty_state,
    }
    response = browser.post(reverse("client-assignment-add", args=[client.pk]), add)
    assert response.status_code == HTTPStatus.FOUND
    with tenant_context(firm.tenant.id):
        responsible = Engagement.objects.get(client=client).responsible_id
        assert responsible == firm.accountant.pk
        assignment = ClientAssignment.objects.get(client=client)
        assert TaxProfile.objects.filter(client=client, regime="mei").exists()
        client.refresh_from_db()
        assigned_state = assignment_state(client)
    remove_url = reverse("client-assignment-remove", args=[client.pk, assignment.pk])
    response = browser.post(remove_url, {"state": assigned_state})
    assert response.status_code == HTTPStatus.FOUND
    # Replaying the old "add" (rendered before the removal) must not undo it, and
    # replaying the old "remove" finds nothing to take away: both are conflicts.
    replayed_add = browser.post(reverse("client-assignment-add", args=[client.pk]), add)
    assert replayed_add.status_code == HTTPStatus.CONFLICT
    replayed_remove = browser.post(remove_url, {"state": assigned_state})
    assert replayed_remove.status_code == HTTPStatus.CONFLICT
    with tenant_context(firm.tenant.id):
        assert not ClientAssignment.objects.filter(client=client).exists()
        assert Event.objects.filter(action=AuditAction.ASSIGNMENT_CHANGED).count() == 2


def test_inviting_a_contact_queues_the_contacts_own_address() -> None:
    firm = _firm("kapa")
    with tenant_context(firm.tenant.pk):
        configure(
            firm.owner,
            kinds=("contact_portal_invite",),
            acknowledged=True,
            expected_version=0,
        )
    client = firm.clients[0]
    browser = firm.as_owner()
    response = browser.post(
        reverse("client-contact-create", args=[client.pk]),
        {
            "contato-name": "Dona Maria",
            "contato-email": "maria@padaria.test",
            "contato-channel_email_allowed": "on",
            "contato-is_active": "on",
        },
    )
    assert response.status_code == HTTPStatus.FOUND
    with tenant_context(firm.tenant.id):
        contact = ClientContact.objects.get(client=client)
    mail.outbox.clear()

    response = browser.post(
        reverse("client-contact-invite", args=[client.pk, contact.pk]),
        {
            "convite-role": TenantRole.CLIENT_OWNER,
            "convite-version": 1,
            "email": "intruso@x.test",
        },
    )

    assert response.status_code == HTTPStatus.FOUND
    assert mail.outbox == []
    with tenant_context(firm.tenant.pk):
        message = OutboundMessage.objects.get()
        assert message.kind == "contact_portal_invite"
        assert message.recipients_json == ["maria@padaria.test"]
    replay = browser.post(
        reverse("client-contact-invite", args=[client.pk, contact.pk]),
        {"convite-role": TenantRole.CLIENT_OWNER, "convite-version": 1},
    )
    # A replayed POST mints no second live credential and sends no second mail.
    assert replay.status_code == HTTPStatus.CONFLICT
    assert mail.outbox == []
    with tenant_context(firm.tenant.pk):
        assert OutboundMessage.objects.count() == 1
    invite = Invite.objects.for_user(firm.owner).get(tenant=firm.tenant)
    assert (invite.email, invite.client_id) == ("maria@padaria.test", client.pk)


def test_a_contact_who_refused_email_cannot_be_invited() -> None:
    firm = _firm("lambda")
    firm.tenant.features = ["import_r1", "cycles_r1"]
    firm.tenant.save(update_fields=["features"])
    client = firm.clients[0]
    with tenant_context(firm.tenant.id):
        configure(
            firm.owner,
            kinds=("contact_portal_invite",),
            acknowledged=True,
            expected_version=0,
        )
        contact = ClientContact.objects.create(
            tenant=firm.tenant,
            client=client,
            name="Sem E-mail",
            email="nao@quer.test",
            channel_email_allowed=False,
        )
    response = firm.as_owner().post(
        reverse("client-contact-invite", args=[client.pk, contact.pk]),
        {"convite-role": TenantRole.CLIENT_OWNER, "convite-version": 1},
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "não tem e-mail autorizado" in response.content.decode()
    assert mail.outbox == []
    assert not Invite.objects.for_user(firm.owner).filter(tenant=firm.tenant).exists()


def test_an_unassigned_staff_accountant_cannot_reach_a_clients_setup() -> None:
    firm = _firm("mu", clients=2)
    mine, theirs = firm.clients
    assign(firm, mine, firm.accountant)
    browser = firm.as_accountant()
    assert browser.get(reverse("client-setup", args=[mine.pk])).status_code == (
        HTTPStatus.OK
    )
    assert browser.get(reverse("client-setup", args=[theirs.pk])).status_code == (
        HTTPStatus.NOT_FOUND
    )
    response = browser.post(
        reverse("client-contact-create", args=[theirs.pk]),
        {"contato-name": "Invasor", "contato-is_active": "on"},
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    with tenant_context(firm.tenant.id):
        assert not ClientContact.objects.exists()
    # Assignment changes need users.create, which a staff accountant lacks.
    response = browser.post(
        reverse("client-assignment-add", args=[mine.pk]),
        {"resp-user": str(firm.accountant.pk), "resp-role": "support"},
    )
    assert response.status_code == HTTPStatus.FORBIDDEN


def test_a_staff_accountant_is_not_offered_the_import() -> None:
    firm = _firm("nu")
    listing = firm.as_accountant().get(reverse("clients-list")).content.decode()
    assert reverse("client-create") in listing
    assert reverse("client-import") not in listing


def test_a_regime_derived_from_the_legacy_flag_is_labelled_unverified() -> None:
    firm = _firm("xi")
    client = firm.clients[0]
    with tenant_context(firm.tenant.id):
        TaxProfile.objects.create(
            tenant=firm.tenant,
            client=client,
            regime=TaxRegime.MEI,
            mei_category="common",
            valid_from=date(2020, 1, 1),
            provenance="backfill_v1",
            confidence=ProfileConfidence.ASSERTED_BY_LEGACY_FLAG,
        )
    page = firm.as_owner().get(reverse("client-setup", args=[client.pk]))
    content = page.content.decode()
    assert "Deduzido do cadastro antigo (não verificado)" in content
    assert "Registrado pela equipe" not in content
