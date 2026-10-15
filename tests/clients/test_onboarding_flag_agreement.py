"""AXR1-007 write paths keep the legacy MEI flag and the tax timeline in agreement.

`tests/clients/test_legacy_flag_agreement.py` (AXR1-006) defines the invariant and
its `disagreements()` tripwire; this module drives every client-creating or
timeline-editing path the onboarding screens and the import add, and runs that same
tripwire after each.
"""

from datetime import date

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from apps.clients.importing import commit_import, plan_import, rollback_import
from apps.clients.maintenance import ClientData, create_client, set_tax_profile
from apps.clients.models import ClientCompany, ProfileConfidence, TaxProfile, TaxRegime
from apps.core.tenancy import tenant_context
from apps.tenants.models import TenantRole
from tests.clients.test_legacy_flag_agreement import disagreements
from tests.ui.factories import add_member, client_base, make_cnpj, make_firm

pytestmark = pytest.mark.django_db(transaction=True)


def test_a_client_created_on_the_screen_opens_its_timeline_with_the_flag() -> None:
    firm = make_firm("acordo")
    ops = add_member(firm.tenant, "ops@acordo.example.com", TenantRole.OPERATIONS_ADMIN)
    with tenant_context(firm.tenant.id):
        # An operations admin holds clients.manage but only LIMITED
        # clients.edit_tax_profile: creating a client must still work.
        client = create_client(
            actor=ops,
            tenant_id=firm.tenant.id,
            data=ClientData(
                legal_name="Nova MEI",
                cnpj=make_cnpj(client_base(1, "acordo")),
                opened_on=date(2022, 5, 1),
            ),
        )
        claim = TaxProfile.objects.get(client=client)
    assert (claim.regime, claim.valid_from, claim.valid_to) == (
        TaxRegime.MEI,
        date(2022, 5, 1),
        None,
    )
    assert claim.confidence == ProfileConfidence.ASSERTED_BY_LEGACY_FLAG
    assert disagreements() == []


def test_imported_clients_agree_and_their_import_claims_do_not_block_rollback() -> None:
    firm = make_firm("importa")
    rows = "\n".join(
        f"{make_cnpj(client_base(i, 'importa'))};Importada {i} MEI" for i in range(3)
    )
    upload = SimpleUploadedFile("c.csv", ("CNPJ;Razão social\n" + rows).encode())
    with tenant_context(firm.tenant.id):
        plan = plan_import(firm.owner, upload, None)
        commit_import(firm.owner, plan.batch_id, expected_version=plan.version)
        claims = TaxProfile.objects.filter(client__import_batch_id=plan.batch_id)
        assert {(c.regime, c.provenance) for c in claims} == {(TaxRegime.MEI, "import")}
        assert claims.count() == 3
    assert disagreements() == []
    with tenant_context(firm.tenant.id):
        result = rollback_import(firm.owner, plan.batch_id, expected_version=2)
    assert result.deleted == 3


def test_a_timeline_edit_on_the_setup_page_moves_the_flag_with_it() -> None:
    firm = make_firm("migra", client_count=1)
    client = firm.clients[0]
    today = timezone.localdate()
    with tenant_context(firm.tenant.id):
        set_tax_profile(
            actor=firm.owner,
            tenant_id=firm.tenant.id,
            client_id=client.pk,
            regime=TaxRegime.MEI,
            mei_category="caminhoneiro",
            valid_from=date(2020, 1, 1),
            expected_revision=1,
        )
        mei = ClientCompany.objects.get(pk=client.pk)
        assert (mei.is_mei, mei.mei_category) == (True, "caminhoneiro")
        set_tax_profile(
            actor=firm.owner,
            tenant_id=firm.tenant.id,
            client_id=client.pk,
            regime=TaxRegime.SIMPLES,
            valid_from=today,
            expected_revision=2,
        )
        moved = ClientCompany.objects.get(pk=client.pk)
    assert moved.is_mei is False
    assert disagreements() == []
