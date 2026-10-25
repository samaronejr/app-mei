"""Termo generation, signed-upload checks and firm submission (AXRM-013 step 3)."""

import base64
import hashlib
import json
from datetime import date, timedelta
from pathlib import Path
from uuid import UUID, uuid4
from xml.etree.ElementTree import Element

import pytest
from defusedxml import ElementTree
from django.core.exceptions import PermissionDenied
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.core.tenancy import tenant_context
from apps.cycles.evidence import DocumentIntake, IntakeState, ScanVerdict
from apps.cycles.openers import TENANT_EVALUATORS, evaluate_tenant_exceptions
from apps.integrations.call_models import FirmProviderCall
from apps.integrations.firm_calls import firm_claim
from apps.integrations.models import ClientProviderLink, FirmAuthorization
from apps.integrations.serpro.authorization import TermoSubmission, submit_termo
from apps.integrations.serpro.codec import Parties
from apps.integrations.serpro.models import ACTIVE_STATES, FirmIntegrationIssue
from apps.integrations.serpro.qualification import SyntheticTermo, synthetic_termo
from apps.integrations.serpro.termo import (
    MAX_XML,
    SIGNATURE_NS,
    LegalNotices,
    SignedTermo,
    TermoDraft,
    TermoInvalidoError,
    read_signed_termo,
    unsigned_termo,
)
from apps.integrations.serpro.termo_state import termo_head, termo_in_force
from apps.integrations.serpro.work import (
    PROC_MISSING,
    UNCERTAIN,
    evaluate_termo_conditions,
    own_firm_condition,
)
from apps.integrations.services import IntegrationConflictError
from apps.integrations.transport_types import TransportFailure
from apps.obligations.models import AcquisitionOrigin, Document
from apps.tenants.models import Membership, TenantRole
from tests.fakes.provider import Reply
from tests.fakes.serpro import FakeSerpro
from tests.integrations.serpro_support import CONTRATANTE, FIRM

pytestmark = pytest.mark.django_db(transaction=True)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "providers" / "serpro"
SYNTHETIC_PROCURADOR = "synthetic-termo-procurador"
NOTICES = LegalNotices(
    "Autorizacao sintetica.", "Somente dados ficticios.", "Qualificacao local."
)
SIGNATURE = (
    f'<Signature xmlns="{SIGNATURE_NS}"><SignedInfo/>'
    "<SignatureValue>AQ==</SignatureValue><KeyInfo><X509Data>"
    "<X509Certificate>AQ==</X509Certificate></X509Data></KeyInfo></Signature>"
).encode()
DENIAL_016 = json.dumps(
    {
        "status": 403,
        "mensagens": [{"codigo": "AcessoNegado-ICGERENCIADOR-016", "texto": "x"}],
        "dados": "{}",
    }
).encode()


def shape(element: Element) -> tuple[str, dict[str, str], list[object]]:
    return element.tag, dict(element.attrib), [shape(child) for child in element]


def signed(unsigned: bytes) -> bytes:
    return unsigned.replace(
        b"</termoDeAutorizacao>", SIGNATURE + b"</termoDeAutorizacao>"
    )


def accepted(expiry: date | None = None) -> Reply:
    until = expiry or timezone.localdate() + timedelta(days=30)
    return Reply(
        body=json.dumps(
            {
                "status": 200,
                "mensagens": [],
                "dados": json.dumps(
                    {
                        "autenticar_procurador_token": SYNTHETIC_PROCURADOR,
                        "data_hora_expiracao": f"{until.isoformat()}T00:00:00",
                    }
                ),
            }
        ).encode()
    )


@pytest.fixture
def world(settings: SettingsWrapper, tmp_path: Path) -> SyntheticTermo:
    settings.MEDIA_ROOT = tmp_path
    settings.PROVIDERS_PAUSED = False
    settings.SERPRO_PAUSED = False
    settings.SERPRO_ENVIRONMENT = "fake"
    settings.PROVIDER_GLOBAL_MONTHLY_CAP_BRL = {"serpro": "0"}
    return synthetic_termo("fake")


def submit(
    world: SyntheticTermo,
    origin: str,
    *,
    version: int = 1,
    actor: User | None = None,
    document_id: UUID | None = None,
) -> str:
    return submit_termo(
        actor or world.actor,
        world.tenant.pk,
        TermoSubmission(
            world.connection.pk,
            document_id or world.document.pk,
            version,
            world.parties,
            origin,
        ),
    ).state


def stage_document(world: SyntheticTermo, raw: bytes) -> UUID:
    with tenant_context(world.tenant.pk):
        key = default_storage.save(f"termo/{uuid4()}.xml", ContentFile(raw))
        document = Document.objects.create(
            tenant=world.tenant,
            client=world.client,
            uploaded_by=world.actor,
            storage_key=key,
            sha256=hashlib.sha256(raw).hexdigest(),
            byte_size=len(raw),
            original_filename="termo.xml",
            content_type="application/xml",
            client_upload=False,
            portal_visible=False,
            acquisition_origin=AcquisitionOrigin.UPLOAD_STAFF,
            retention_class="operational",
        )
        DocumentIntake.objects.create(
            tenant=world.tenant,
            client=world.client,
            document=document,
            state=IntakeState.VERIFIED,
            verdict=ScanVerdict.CLEAN,
            scan_engine="synthetic",
            scanned_at=timezone.now(),
        )
        return document.pk


def active_issues(world: SyntheticTermo) -> list[str]:
    with tenant_context(world.tenant.pk):
        return list(
            FirmIntegrationIssue.objects.filter(state__in=ACTIVE_STATES).values_list(
                "reason", flat=True
            )
        )


def test_unsigned_termo_reproduces_the_pinned_official_structure() -> None:
    # Given the structure pinned from the official AUTENTICAPROCURADOR model.
    fixture = ElementTree.fromstring((FIXTURES / "termo_unsigned.xml").read_bytes())
    texts = {child.tag: child.attrib for child in fixture[0]}
    draft = TermoDraft(
        "Contratante sintetico",
        "Escritorio sintetico",
        date(2026, 10, 25),
        date(2027, 10, 25),
        LegalNotices(
            texts["termo"]["texto"],
            texts["avisoLegal"]["texto"],
            texts["finalidade"]["texto"],
        ),
    )
    # When Axorim generates the draft the firm signs outside Axorim.
    generated = ElementTree.fromstring(
        unsigned_termo(Parties(CONTRATANTE, FIRM, FIRM), draft)
    )
    # Then every tag, attribute and order matches, and nothing is signed here.
    assert shape(generated) == shape(fixture)
    assert generated.find(f"{{{SIGNATURE_NS}}}Signature") is None


def _draft(signed_on: date, valid_until: date) -> bytes:
    return unsigned_termo(
        Parties(CONTRATANTE, FIRM, FIRM),
        TermoDraft("Contratante", "Escritorio", signed_on, valid_until, NOTICES),
    )


VALID = _draft(date(2026, 10, 25), date(2027, 10, 25))


@pytest.mark.parametrize(
    "raw",
    [
        VALID,
        signed(VALID.replace(FIRM.encode(), b"11444777000161")),
        signed(
            VALID.replace(
                b"<termoDeAutorizacao>",
                b'<!DOCTYPE termoDeAutorizacao [<!ENTITY x "y">]><termoDeAutorizacao>',
                1,
            )
        ),
        signed(VALID) + b" " * MAX_XML,
        signed(VALID.replace(b"20271025", b"20251025")),
        signed(VALID).replace(b"<SignatureValue>AQ==", b"<SignatureValue>!!", 1),
    ],
    ids=["unsigned", "other-firm", "dtd", "oversized", "inverted-dates", "bad-b64"],
)
def test_signed_upload_is_checked_before_any_submission(raw: bytes) -> None:
    # Given an upload that is unsigned, misattributed, hostile or malformed.
    # When the boundary inspects it, then it is refused without echoing contents.
    with pytest.raises(TermoInvalidoError):
        read_signed_termo(raw, Parties(CONTRATANTE, FIRM, FIRM))
    assert read_signed_termo(
        signed(VALID), Parties(CONTRATANTE, FIRM, FIRM)
    ) == SignedTermo(date(2026, 10, 25), date(2027, 10, 25))


def test_second_caller_with_the_rendered_version_conflicts_without_writes(
    world: SyntheticTermo,
) -> None:
    # Given two operators who rendered the same connection version.
    with FakeSerpro((accepted(), accepted())) as peer:
        first = submit(world, peer.origin)
        # When the second submits after the first one committed.
        with pytest.raises(IntegrationConflictError):
            submit(world, peer.origin)
        # Then only one signed termo reached SERPRO.
        assert len(peer.requests) == 1
    assert first == "sucesso"
    with tenant_context(world.tenant.pk):
        assert FirmProviderCall.objects.count() == 1
        assert sorted(FirmAuthorization.objects.values_list("status", flat=True)) == [
            "aceito",
            "enviado",
        ]


def test_staff_without_authority_capability_is_refused_without_rows(
    world: SyntheticTermo,
) -> None:
    # Given a staff accountant (fiscal.authority.manage is Owner/Ops only).
    staff = User.objects.create_user(email=f"staff-{uuid4().hex}@example.test")
    Membership.objects.create(
        tenant=world.tenant, user=staff, role=TenantRole.STAFF_ACCOUNTANT
    )
    with FakeSerpro((accepted(),)) as peer:
        # When the staff member tries to submit, then the capability refuses first.
        with pytest.raises(PermissionDenied):
            submit(world, peer.origin, actor=staff)
        assert peer.requests == []
    with tenant_context(world.tenant.pk):
        assert not FirmAuthorization.objects.exists()
        assert not FirmProviderCall.objects.exists()


def test_unscanned_upload_is_refused_before_any_submission(
    world: SyntheticTermo,
) -> None:
    # Given the signed XML is still in quarantine.
    with tenant_context(world.tenant.pk):
        DocumentIntake.objects.filter(document=world.document).update(
            state=IntakeState.QUARANTINED, scanned_at=None
        )
    with FakeSerpro((accepted(),)) as peer:
        # When an operator submits it, then nothing is read, recorded or sent.
        with pytest.raises(TermoInvalidoError):
            submit(world, peer.origin)
        assert peer.requests == []
    with tenant_context(world.tenant.pk):
        assert not FirmAuthorization.objects.exists()


def test_accepted_termo_records_its_validity_and_resolves_firm_work(
    world: SyntheticTermo,
) -> None:
    # Given owned firm work for a missing procurador authorization.
    with tenant_context(world.tenant.pk):
        own_firm_condition(world.connection, PROC_MISSING, [])
    expiry = timezone.localdate() + timedelta(days=30)
    with FakeSerpro((accepted(expiry=expiry),)) as peer:
        # When the scanned signed termo is submitted through the firm journal.
        assert submit(world, peer.origin) == "sucesso"
        [request] = peer.requests
        # Then the request carries exactly the stored signed bytes, base64-encoded.
        assert (request.method, request.path) == ("POST", "/Apoiar")
        dados = json.loads(json.loads(request.body)["pedidoDados"]["dados"])
        with default_storage.open(world.document.storage_key, "rb") as stored:
            assert base64.b64decode(dados["xml"]) == stored.read()
    with tenant_context(world.tenant.pk):
        head = termo_head(world.connection.pk)
        assert head is not None
        # The shorter of the termo's vigencia and the provider's token expiry.
        assert (head.status, head.valid_until) == ("aceito", expiry)
        assert termo_in_force(world.connection.pk, timezone.localdate())
    assert active_issues(world) == []


@pytest.mark.parametrize(
    "reply",
    [
        Reply(status=403, body=DENIAL_016),
        Reply(status=400, body=b'{"status":400,"mensagens":[],"dados":"{}"}'),
    ],
    ids=["403-016", "400"],
)
def test_refused_termo_is_recorded_and_owned_by_the_firm(
    world: SyntheticTermo, reply: Reply
) -> None:
    # Given SERPRO refuses the signed termo.
    with FakeSerpro((reply,)) as peer:
        # When it is submitted, then it is sent once and never retried.
        assert submit(world, peer.origin) in {"negada", "falhou"}
        assert len(peer.requests) == 1
    with tenant_context(world.tenant.pk):
        head = termo_head(world.connection.pk)
        assert head is not None
        assert head.status == "recusado"
        assert not termo_in_force(world.connection.pk, timezone.localdate())
        issue = FirmIntegrationIssue.objects.get(state="aberta")
        assert (issue.reason, issue.owner_id) == (PROC_MISSING, world.actor.pk)


def test_malformed_successful_termo_is_owned_without_another_submission(
    world: SyntheticTermo,
) -> None:
    # Given a complete 200 without the promised token or expiration fields.
    malformed = Reply(body=b'{"status":200,"mensagens":[],"dados":"{}"}')
    with FakeSerpro((malformed, accepted())) as peer:
        # When the normal signed-termo service cannot complete domain acceptance.
        with pytest.raises(TransportFailure) as failure:
            submit(world, peer.origin)
        assert failure.value.code == "resposta_termo_invalida"
        assert len(peer.requests) == 1
    # Then its unresolved outcome is owned, never accepted or automatically resent.
    with tenant_context(world.tenant.pk):
        call = FirmProviderCall.objects.get()
        assert not FirmAuthorization.objects.filter(status="aceito").exists()
        issues = list(FirmIntegrationIssue.objects.filter(state__in=ACTIVE_STATES))
        assert len(issues) == 1
        [issue] = issues
        assert (issue.reason, issue.owner_id) == (UNCERTAIN, world.actor.pk)
        assert issue.connection_id == world.connection.pk
        assert issue.evidence_refs == [str(call.pk)]
    assert firm_claim(world.tenant.pk, call.pk) is None


def test_expired_termo_opens_firm_work_and_sends_nothing(world: SyntheticTermo) -> None:
    # Given a correctly signed termo whose vigencia already ended.
    today = timezone.localdate()
    raw = signed(
        unsigned_termo(
            world.parties,
            TermoDraft(
                "Contratante",
                "Escritorio",
                today - timedelta(days=400),
                today - timedelta(days=35),
                NOTICES,
            ),
        )
    )
    document_id = stage_document(world, raw)
    with FakeSerpro((accepted(),)) as peer:
        # When an operator submits it, then the refusal precedes any submission.
        with pytest.raises(TermoInvalidoError):
            submit(world, peer.origin, document_id=document_id)
        assert peer.requests == []
    assert active_issues(world) == [PROC_MISSING]
    with tenant_context(world.tenant.pk):
        assert not FirmAuthorization.objects.exists()


def sweep(world: SyntheticTermo, today: date) -> None:
    with tenant_context(world.tenant.pk):
        evaluate_tenant_exceptions(world.tenant, today=today)


def test_daily_sweep_owns_a_missing_or_expired_termo_for_a_connection_in_use(
    world: SyntheticTermo,
) -> None:
    today = timezone.localdate()
    # Given a SERPRO connection that no client uses yet: nothing to own.
    assert evaluate_termo_conditions in TENANT_EVALUATORS
    sweep(world, today)
    assert active_issues(world) == []
    # When a client link starts using it without a termo, the daily openers own it.
    with tenant_context(world.tenant.pk):
        ClientProviderLink.objects.create(
            tenant=world.tenant,
            client=world.client,
            connection=world.connection,
            provider_company_ref="synthetic-sweep",
            status="ativa",
        )
    sweep(world, today)
    sweep(world, today)
    assert active_issues(world) == [PROC_MISSING]
    # Then an accepted termo resolves it until its validity ends.
    expiry = today + timedelta(days=30)
    with FakeSerpro((accepted(expiry=expiry),)) as peer:
        assert submit(world, peer.origin) == "sucesso"
    sweep(world, expiry)
    assert active_issues(world) == []
    sweep(world, expiry + timedelta(days=1))
    assert active_issues(world) == [PROC_MISSING]
