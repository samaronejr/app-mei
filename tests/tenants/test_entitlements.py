"""Client ceilings apply at admission, not only to the settings display."""

from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from threading import Barrier

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.audit.models import AuditAction, Event
from apps.clients.importing import commit_import, plan_import, queue_commit
from apps.clients.maintenance import ClientData, create_client, update_client
from apps.clients.models import ClientCompany, ClientStatus, ImportBatch, ImportStatus
from apps.clients.services import save_client_registry
from apps.core.tenancy import tenant_context
from tests.clients.test_import_wizard import csv_upload
from tests.support_threads import run_in_thread_as_runtime
from tests.ui.factories import Firm, add_client, client_base, make_cnpj, make_firm

pytestmark = pytest.mark.django_db(transaction=True)


def limited_firm(slug: str, *, limit: int | None, clients: int = 1) -> Firm:
    firm = make_firm(slug, client_count=clients)
    firm.tenant.features = ["import_r1"]
    firm.tenant.active_client_limit = limit
    firm.tenant.save(update_fields=["features", "active_client_limit"])
    return firm


def test_over_limit_create_is_a_soft_refusal_without_writes() -> None:
    firm = limited_firm("entitlement-http", limit=1)
    response = firm.as_owner().post(
        reverse("client-create"),
        {
            "legal_name": "Cliente adicional",
            "cnpj": make_cnpj(client_base(2, firm.tenant.slug)),
        },
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "limite" in response.content.decode().lower()
    with tenant_context(firm.tenant.pk):
        assert ClientCompany.objects.count() == 1
        assert not Event.objects.filter(action=AuditAction.CLIENT_CREATED).exists()


def test_the_last_slot_is_available_and_onboarding_consumes_it() -> None:
    firm = limited_firm("entitlement-last-slot", limit=2)
    with tenant_context(firm.tenant.pk):
        first = create_client(
            actor=firm.owner,
            tenant_id=firm.tenant.pk,
            data=ClientData("Em implantação", make_cnpj(client_base(2, "last-slot"))),
        )
        with pytest.raises(ValidationError) as refused:
            create_client(
                actor=firm.owner,
                tenant_id=firm.tenant.pk,
                data=ClientData("Excedente", make_cnpj(client_base(3, "last-slot"))),
            )
        assert first.status == ClientStatus.ONBOARDING
        assert refused.value.code == "active_client_limit"
        assert ClientCompany.objects.count() == 2
        assert Event.objects.filter(action=AuditAction.CLIENT_CREATED).count() == 1


@pytest.mark.parametrize("limit", [None, 0])
def test_zero_is_not_an_unlimited_contract(limit: int | None) -> None:
    firm = limited_firm("entitlement-zero", limit=limit, clients=0)
    with tenant_context(firm.tenant.pk):
        data = ClientData("Primeiro cliente", make_cnpj(client_base(1, "zero")))
        if limit is None:
            create_client(actor=firm.owner, tenant_id=firm.tenant.pk, data=data)
            assert ClientCompany.objects.count() == 1
        else:
            with pytest.raises(ValidationError) as refused:
                create_client(actor=firm.owner, tenant_id=firm.tenant.pk, data=data)
            assert refused.value.code == "active_client_limit"
            assert ClientCompany.objects.count() == 0


def test_closed_clients_do_not_consume_slots_but_reactivation_does() -> None:
    firm = limited_firm("entitlement-reactivate", limit=1)
    closed = add_client(
        firm,
        legal_name="Encerrado",
        base=client_base(2, "reactivate"),
        status=ClientStatus.CLOSED,
    )
    with tenant_context(firm.tenant.pk):
        with pytest.raises(ValidationError) as refused:
            update_client(
                actor=firm.owner,
                tenant_id=firm.tenant.pk,
                client_id=closed.pk,
                expected_version=closed.version,
                changes={"status": ClientStatus.ACTIVE},
            )
        assert refused.value.code == "active_client_limit"
        closed.refresh_from_db()
        assert (closed.status, closed.version) == (ClientStatus.CLOSED, 1)
        assert not Event.objects.filter(action=AuditAction.CLIENT_UPDATED).exists()


def test_another_firms_active_clients_do_not_consume_our_slot() -> None:
    make_firm("entitlement-other", client_count=3)
    firm = limited_firm("entitlement-own", limit=1, clients=0)
    with tenant_context(firm.tenant.pk):
        client = create_client(
            actor=firm.owner,
            tenant_id=firm.tenant.pk,
            data=ClientData("Nosso", make_cnpj(client_base(1, "own-slot"))),
        )
        assert list(ClientCompany.objects.values_list("pk", flat=True)) == [client.pk]


def test_import_dry_run_shows_limit_and_refuses_an_oversized_commit() -> None:
    firm = limited_firm("entitlement-preview", limit=2)
    browser = firm.as_owner()
    rows = [
        (make_cnpj(client_base(i, "preview")), f"Cliente {i}", "SP") for i in (2, 3)
    ]
    response = browser.post(reverse("client-import"), {"file": csv_upload(rows)})
    assert response.status_code == HTTPStatus.FOUND
    preview = browser.get(response["Location"])
    assert preview.status_code == HTTPStatus.OK
    assert 'data-active-client-limit="2"' in preview.content.decode()
    assert 'data-active-client-remaining="1"' in preview.content.decode()
    with tenant_context(firm.tenant.pk):
        batch = ImportBatch.objects.get()
    refused = browser.post(
        reverse("client-import-commit", args=[batch.pk]), {"version": batch.version}
    )
    assert refused.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "limite" in refused.content.decode().lower()
    with tenant_context(firm.tenant.pk):
        batch.refresh_from_db()
        assert (batch.status, batch.version) == (ImportStatus.PLANNED, 1)
        assert ClientCompany.objects.count() == 1
        assert not Event.objects.filter(action=AuditAction.IMPORT_QUEUED).exists()


def test_commit_rechecks_capacity_consumed_after_the_preview() -> None:
    firm = limited_firm("entitlement-stale-preview", limit=2)
    with tenant_context(firm.tenant.pk):
        plan = plan_import(
            firm.owner,
            csv_upload(
                [(make_cnpj(client_base(2, "stale-preview")), "Importado", "SP")]
            ),
            None,
        )
        queued = queue_commit(firm.owner, plan.batch_id, expected_version=plan.version)
        create_client(
            actor=firm.owner,
            tenant_id=firm.tenant.pk,
            data=ClientData("Chegou antes", make_cnpj(client_base(3, "stale-preview"))),
        )
        with pytest.raises(ValidationError) as refused:
            commit_import(firm.owner, plan.batch_id, expected_version=queued.version)
        assert refused.value.code == "active_client_limit"
        assert not ClientCompany.objects.filter(import_batch_id=plan.batch_id).exists()
        assert not Event.objects.filter(action=AuditAction.IMPORT_COMMITTED).exists()


def test_duplicates_added_since_preview_require_no_new_slots() -> None:
    firm = limited_firm("entitlement-duplicate", limit=2)
    cnpj = make_cnpj(client_base(2, "duplicate"))
    with tenant_context(firm.tenant.pk):
        plan = plan_import(
            firm.owner, csv_upload([(cnpj, "Mesmo cliente", "SP")]), None
        )
        create_client(
            actor=firm.owner,
            tenant_id=firm.tenant.pk,
            data=ClientData("Já cadastrado", cnpj),
        )
        queued = queue_commit(firm.owner, plan.batch_id, expected_version=plan.version)
        result = commit_import(
            firm.owner, plan.batch_id, expected_version=queued.version
        )
        assert (result.created, result.skipped, result.failed) == (0, 1, 0)
        assert ClientCompany.objects.count() == 2


def test_legacy_registry_cannot_bypass_the_ceiling() -> None:
    firm = limited_firm("entitlement-legacy", limit=1)
    company = ClientCompany(
        tenant=firm.tenant,
        legal_name="Cadastro antigo",
        cnpj=make_cnpj(client_base(2, "legacy")),
    )
    with tenant_context(firm.tenant.pk):
        with pytest.raises(ValidationError) as refused:
            save_client_registry(actor=firm.owner, client=company, created=True)
        assert refused.value.code == "active_client_limit"
        assert not ClientCompany.objects.filter(pk=company.pk).exists()


def test_two_concurrent_creates_cannot_both_claim_the_last_slot() -> None:
    firm = limited_firm("entitlement-race", limit=1, clients=0)
    ready = Barrier(2)

    def attempt(number: int) -> str:
        def admit() -> str:
            ready.wait(timeout=10)
            try:
                create_client(
                    actor=firm.owner,
                    tenant_id=firm.tenant.pk,
                    data=ClientData(
                        f"Concorrente {number}",
                        make_cnpj(client_base(number, "race")),
                    ),
                )
            except ValidationError as error:
                return str(error.code)
            return "created"

        return run_in_thread_as_runtime(firm.tenant.pk, admit)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, (1, 2)))
    assert sorted(outcomes) == ["active_client_limit", "created"]
    with tenant_context(firm.tenant.pk):
        assert ClientCompany.objects.count() == 1
        assert Event.objects.filter(action=AuditAction.CLIENT_CREATED).count() == 1
