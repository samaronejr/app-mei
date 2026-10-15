"""Real import and contact UI, outbox SMTP refresh, and portal sign-in setup."""

import re
from dataclasses import dataclass
from datetime import timedelta
from http import HTTPStatus

from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.clients.models import ClientCompany, ClientContact, Engagement, ImportBatch
from apps.core.tenancy import tenant_context
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.models import PlaybookVersion, ServiceCycle
from apps.cycles.packs import install_system_packs
from apps.cycles.services.cycle import open_cycle
from apps.cycles.services.requests import RequestFields, create_request, send_requests
from apps.delivery.models import OutboundMessage
from apps.delivery.tasks import dispatch_outbox
from apps.fiscal.mei import MEICategory
from apps.tenants.models import TenantRole
from tests.clients.contact_invite_fixtures import EMAIL
from tests.clients.test_import_wizard import cnpj, csv_upload
from tests.cycles.cycle_fixtures import PERIOD
from tests.delivery.smtp import FakeSMTP
from tests.support import pinned_totp_window, totp_code
from tests.ui.factories import PASSWORD, Firm, add_member, assign, make_firm

TOKEN = re.compile(rb"/convites/aceitar/([A-Za-z0-9_\-]+)/")
PORTAL_URLCONF = "apps.portal.urls"


@dataclass(frozen=True, slots=True)
class Journey:
    firm: Firm
    contact: ClientContact
    cycle: ServiceCycle
    first_token: str
    refreshed_token: str


def _dispatch_token() -> str:
    with (
        FakeSMTP() as smtp,
        override_settings(
            EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
            EMAIL_HOST="127.0.0.1",
            EMAIL_PORT=smtp.port,
            EMAIL_HOST_USER="",
            EMAIL_HOST_PASSWORD="",
            EMAIL_USE_TLS=False,
            EMAIL_USE_SSL=False,
            EMAIL_TIMEOUT=3,
        ),
    ):
        dispatch_outbox()
    assert len(smtp.received) == 1
    match = TOKEN.search(smtp.received[0])
    assert match is not None
    return match.group(1).decode("ascii")


def contact_journey(settings: SettingsWrapper) -> Journey:
    """Use the real UI and tasks to offer and refresh an imported client's invite."""
    settings.DELIVERY_PAUSED = False
    settings.CELERY_TASK_ALWAYS_EAGER = True
    settings.CELERY_TASK_EAGER_PROPAGATES = True
    firm = make_firm("contact-journey", client_count=1)
    firm.tenant.features = ["import_r1", "cycles_r1"]
    firm.tenant.save(update_fields=["features"])
    staff = firm.as_owner()
    imported = staff.post(
        reverse("client-import"),
        {"file": csv_upload([(cnpj(4, "contact-journey"), "Imported MEI", "SP")])},
    )
    assert imported.status_code == HTTPStatus.FOUND
    with tenant_context(firm.tenant.pk):
        batch = ImportBatch.objects.get()
    committed = staff.post(
        reverse("client-import-commit", args=[batch.pk]),
        {"version": batch.version},
    )
    assert committed.status_code == HTTPStatus.FOUND
    with tenant_context(firm.tenant.pk):
        client = ClientCompany.objects.get(import_batch=batch)
        install_system_packs(firm.tenant)
    activated = staff.post(
        reverse("client-edit", args=[client.pk]),
        {
            "legal_name": client.legal_name,
            "status": "active",
            "version": client.version,
        },
    )
    assert activated.status_code == HTTPStatus.FOUND
    with tenant_context(firm.tenant.pk):
        client.refresh_from_db()
    profiled = staff.post(
        reverse("client-tax-profile", args=[client.pk]),
        {
            "tax-regime": "mei",
            "tax-mei_category": MEICategory.COMMON,
            "tax-valid_from": PERIOD.isoformat(),
            "tax-expected_revision": client.tax_profile_revision,
            "tax-note": "Configuração do serviço para a competência de setembro.",
        },
    )
    assert profiled.status_code == HTTPStatus.FOUND
    engaged = staff.post(
        reverse("client-engagement-create", args=[client.pk]),
        {"eng-service_kind": "mei_mensal", "eng-starts_on": PERIOD.isoformat()},
    )
    assert engaged.status_code == HTTPStatus.FOUND
    created = staff.post(
        reverse("client-contact-create", args=[client.pk]),
        {
            "contato-name": "Invited contact",
            "contato-email": EMAIL,
            "contato-channel_email_allowed": "on",
            "contato-is_active": "on",
        },
    )
    assert created.status_code == HTTPStatus.FOUND
    with tenant_context(firm.tenant.pk):
        contact = ClientContact.objects.get(client=client)
    authorized = staff.post(
        reverse("notice-policy"),
        {
            "action": "save",
            "expected_version": 0,
            "kinds": ["contact_portal_invite"],
            "acknowledged": "on",
        },
    )
    assert authorized.status_code == HTTPStatus.FOUND
    invited = staff.post(
        reverse("client-contact-invite", args=[client.pk, contact.pk]),
        {"convite-role": TenantRole.CLIENT_OWNER, "convite-version": contact.version},
    )
    assert invited.status_code == HTTPStatus.FOUND
    first = _dispatch_token()
    with tenant_context(firm.tenant.pk):
        assign(firm, client, firm.accountant)
        engagement = Engagement.objects.get(client=client)
        definition = PlaybookVersion.objects.get(
            playbook__key="mei-mensal", state="published"
        )
        ref = open_cycle(
            firm.owner,
            engagement.pk,
            PERIOD,
            definition.pk,
            idempotency_key="contact-journey",
        )
        create_request(
            firm.owner,
            ref.id,
            RequestFields(title="XML de setembro"),
            expected_version=ref.version,
        )
        cycle = ServiceCycle.objects.get(pk=ref.id)
        send_requests(firm.owner, cycle.pk, expected_version=cycle.version)
        cycle.refresh_from_db()
        invitation = OutboundMessage.objects.get(kind="contact_portal_invite")
        request_mail = OutboundMessage.objects.get(kind="request_notice")
        assert request_mail.invite_id == invitation.invite_id
        assert request_mail.contact_id == invitation.contact_id == contact.pk
    refreshed = _dispatch_token()
    assert first != refreshed
    return Journey(firm, contact, cycle, first, refreshed)


def portal_session(journey: Journey, *, existing: bool) -> Client:
    """Sign in an existing portal account through real password and MFA routes."""
    host = f"{journey.firm.tenant.slug}-portal.localhost"
    portal = Client(SERVER_NAME=host)
    if existing:
        add_member(
            journey.firm.tenant,
            EMAIL,
            TenantRole.CLIENT_OWNER,
            client=journey.firm.clients[0],
        )
        logged_in = portal.post(
            reverse("account_login", urlconf=PORTAL_URLCONF),
            {"login": EMAIL, "password": PASSWORD},
        )
        assert logged_in.status_code == HTTPStatus.FOUND
        with pinned_totp_window():
            mfa = portal.post(
                reverse("mfa_authenticate", urlconf=PORTAL_URLCONF),
                {"code": totp_code()},
            )
            assert mfa.status_code == HTTPStatus.FOUND
    return portal


def accept_path(token: str) -> str:
    """The actual unconfined portal acceptance route."""
    return reverse("portal-invite-accept", args=[token], urlconf=PORTAL_URLCONF)


def publication_draft(contact: ClientContact) -> ProposalDraft:
    """Select the invited contact for the real exact-effect publication builder."""
    return ProposalDraft(
        ProposalInput(
            action_kind=ActionKind.CLIENT_PUBLICATION,
            step_key="publication",
            recipient_ids=(contact.pk,),
            subject="Documentos de setembro",
            body="Documentos revisados para conferência.",
        ),
        timezone.now() + timedelta(days=1),
    )
