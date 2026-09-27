"""An imported client with a cycle is no longer disposable import scaffolding."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.clients.importing import (
    RollbackRefusedError,
    commit_import,
    plan_import,
    rollback_import,
)
from apps.clients.models import ClientCompany, Engagement, ImportBatch, TaxProfile
from apps.core.tenancy import tenant_context
from apps.cycles.models import ServiceCycle
from apps.cycles.services.cycle import open_cycle
from tests.cycles.cycle_fixtures import PERIOD, cycle_setup

pytestmark = pytest.mark.django_db(transaction=True)


def test_import_rollback_refuses_and_lists_service_cycle() -> None:
    setup = cycle_setup()
    with tenant_context(setup.tenant.pk):
        planned = plan_import(
            setup.actor,
            SimpleUploadedFile(
                "clients.csv",
                b"CNPJ;Razao social;UF\n11444777000161;Imported synthetic;SP",
                content_type="text/csv",
            ),
            {"cnpj": "CNPJ", "legal_name": "Razao social", "state": "UF"},
        )
        commit_import(setup.actor, planned.batch_id, expected_version=1)
        batch = ImportBatch.objects.get(pk=planned.batch_id)
        client = ClientCompany.objects.get(import_batch=batch)
        client.status = "active"
        client.save(update_fields=["status"])
        engagement = Engagement.objects.create(
            tenant=setup.tenant,
            client=client,
            service_kind="mei_mensal",
            starts_on=PERIOD,
            provenance="staff_entry",
        )
        TaxProfile.objects.create(
            tenant=setup.tenant,
            client=client,
            regime="mei",
            valid_from=PERIOD,
            provenance="staff_entry",
        )
        ref = open_cycle(
            setup.actor,
            engagement.pk,
            PERIOD,
            setup.definition.pk,
            idempotency_key="import-cycle",
        )
        with pytest.raises(RollbackRefusedError) as caught:
            rollback_import(setup.actor, batch.pk, expected_version=batch.version)
        blocking = [
            row for row in caught.value.dependents if row.kind == "cycles.servicecycle"
        ]
        assert len(blocking) == 1
        assert (blocking[0].client_id, blocking[0].count) == (client.pk, 1)
        assert ServiceCycle.objects.filter(pk=ref.id).exists()
        assert ClientCompany.objects.filter(pk=client.pk).exists()
