"""The termo operator command keeps the authority capability and owned-work scope."""

import io
import json
from pathlib import Path
from uuid import uuid4

import pytest
from defusedxml import ElementTree
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.core.tenancy import tenant_context
from apps.integrations.models import ProviderConnection
from apps.integrations.serpro.models import FirmIntegrationIssue
from apps.integrations.serpro.qualification import SyntheticTermo, synthetic_termo
from apps.integrations.serpro.work import AUTH_FAILED, PROC_MISSING, own_firm_condition
from apps.tenants.models import Membership, TenantRole
from tests.integrations.serpro_support import CONTRATANTE, FIRM

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def world(settings: SettingsWrapper, tmp_path: Path) -> SyntheticTermo:
    settings.MEDIA_ROOT = tmp_path
    return synthetic_termo("fake")


def run(world: SyntheticTermo, action: str, actor: User, *extra: str) -> str:
    stdout = io.StringIO()
    call_command(
        "serpro_authorization",
        action,
        "--tenant",
        str(world.tenant.pk),
        "--actor",
        str(actor.pk),
        "--connection",
        str(world.connection.pk),
        *extra,
        stdout=stdout,
    )
    return stdout.getvalue()


def test_issues_lists_only_this_connections_active_firm_work(
    world: SyntheticTermo,
) -> None:
    # Given active work here, resolved work here, and another connection's work.
    with tenant_context(world.tenant.pk):
        own_firm_condition(world.connection, PROC_MISSING, [])
        own_firm_condition(world.connection, AUTH_FAILED, [])
        FirmIntegrationIssue.objects.filter(reason=AUTH_FAILED).update(
            state="resolvida"
        )
        other = ProviderConnection.objects.create(
            tenant=world.tenant, provider="serpro", environment="trial", status="ativa"
        )
        own_firm_condition(other, PROC_MISSING, [])
        active = FirmIntegrationIssue.objects.get(
            connection=world.connection, reason=PROC_MISSING
        )
    # When an operator lists the open firm work of this connection.
    rows = [json.loads(line) for line in run(world, "issues", world.actor).splitlines()]
    # Then exactly the active issue is listed, with its owner and review date.
    assert rows == [
        {
            "id": str(active.pk),
            "reason": PROC_MISSING,
            "owner_id": str(world.actor.pk),
            "review_on": str(timezone.localdate()),
            "version": 1,
        }
    ]


def test_staff_accountant_cannot_use_the_authority_command(
    world: SyntheticTermo,
) -> None:
    # Given a staff accountant (fiscal.authority.manage is Owner/Ops only).
    staff = User.objects.create_user(email=f"staff-{uuid4().hex}@example.test")
    Membership.objects.create(
        tenant=world.tenant, user=staff, role=TenantRole.STAFF_ACCOUNTANT
    )
    with tenant_context(world.tenant.pk):
        own_firm_condition(world.connection, PROC_MISSING, [])
    # When the staff member asks for firm work, then the capability refuses it.
    with pytest.raises(PermissionDenied):
        run(world, "issues", staff)


def test_prepare_prints_an_unsigned_termo_for_signing_outside_axorim(
    world: SyntheticTermo, tmp_path: Path
) -> None:
    # Given the firm's current official legal texts.
    notices = tmp_path / "notices.json"
    notices.write_text(
        json.dumps(
            {
                "authorization": "Autorizacao sintetica.",
                "privacy": "Somente dados ficticios.",
                "purpose": "Qualificacao local.",
            }
        )
    )
    # When an operator prepares the termo for the firm to sign.
    printed = run(
        world,
        "prepare",
        world.actor,
        "--contratante",
        CONTRATANTE,
        "--firm",
        FIRM,
        "--contratante-name",
        "Contratante sintetico",
        "--firm-name",
        "Escritorio sintetico",
        "--valid-until",
        "2027-10-25",
        "--legal-notices",
        str(notices),
    )
    # Then it is the published structure with no Signature: nothing is signed here.
    root = ElementTree.fromstring(printed.encode())
    data = root.find("dados")
    assert root.tag == "termoDeAutorizacao"
    assert [child.tag for child in root] == ["dados"]
    assert data is not None
    assert [child.tag for child in data] == [
        "sistema",
        "termo",
        "avisoLegal",
        "finalidade",
        "dataAssinatura",
        "vigencia",
        "destinatario",
        "assinadoPor",
    ]
    assert data[4].get("data") == timezone.localdate().strftime("%Y%m%d")
    assert data[5].get("data") == "20271025"
    assert (data[6].get("numero"), data[7].get("numero")) == (CONTRATANTE, FIRM)
