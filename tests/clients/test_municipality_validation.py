"""Municipality registry validation on client forms, imports, and workspace."""

from http import HTTPStatus

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.clients.forms import ClientCreateForm, ClientEditForm
from apps.clients.importing import commit_import, plan_import
from apps.clients.models import ClientCompany, ImportRow, RowOutcome
from apps.core.tenancy import tenant_context
from apps.fiscal.models import Municipality
from tests.ui.factories import client_base, make_cnpj, make_firm

pytestmark = pytest.mark.django_db(transaction=True)

NITEROI = "3303302"
UNKNOWN_MUNICIPALITY = "3399999"
UNKNOWN_CODE_MESSAGE = "Município não encontrado no cadastro (código IBGE)"


def _form_data(
    form_type: type[ClientCreateForm] | type[ClientEditForm],
    code: str,
) -> dict[str, str]:
    data = {
        "legal_name": "Cliente de teste",
        "trade_name": "",
        "cpf": "",
        "state": "RJ",
        "municipality_ibge_code": code,
        "main_cnae": "",
        "opened_on": "",
    }
    if form_type is ClientCreateForm:
        base = client_base(1, "municipio-form")
        data["cnpj"] = make_cnpj(base)
    else:
        data.update({"status": "active", "version": "1"})
    return data


@pytest.mark.parametrize("form_type", [ClientCreateForm, ClientEditForm])
def test_client_forms_reject_municipality_outside_the_registry(
    form_type: type[ClientCreateForm] | type[ClientEditForm],
) -> None:
    form = form_type(data=_form_data(form_type, UNKNOWN_MUNICIPALITY))

    assert not form.is_valid()
    assert str(form.errors["municipality_ibge_code"][0]) == UNKNOWN_CODE_MESSAGE


@pytest.mark.parametrize("form_type", [ClientCreateForm, ClientEditForm])
def test_client_forms_accept_registered_niteroi(
    form_type: type[ClientCreateForm] | type[ClientEditForm],
) -> None:
    form = form_type(data=_form_data(form_type, NITEROI))

    assert form.is_valid(), form.errors
    assert form.cleaned_data["municipality_ibge_code"] == NITEROI


@pytest.mark.parametrize("form_type", [ClientCreateForm, ClientEditForm])
def test_client_form_accepts_an_empty_municipality_code(
    form_type: type[ClientCreateForm] | type[ClientEditForm],
) -> None:
    form = form_type(data=_form_data(form_type, ""))

    assert form.is_valid(), form.errors
    assert form.cleaned_data["municipality_ibge_code"] == ""


def _csv_upload(code: str) -> SimpleUploadedFile:
    content = (
        "CNPJ;Razão social;UF;IBGE\n"
        f"{make_cnpj(client_base(2, 'municipio-import'))};Cliente importado;RJ;{code}\n"
    )
    return SimpleUploadedFile("clientes.csv", content.encode(), content_type="text/csv")


def test_import_refuses_unknown_municipality_without_creating_a_client() -> None:
    firm = make_firm("municipio-import-reject")
    with tenant_context(firm.tenant.id):
        plan = plan_import(firm.owner, _csv_upload(UNKNOWN_MUNICIPALITY), None)
        row = ImportRow.objects.get(batch_id=plan.batch_id)

        assert (plan.created, plan.invalid) == (0, 1)
        assert row.outcome == RowOutcome.INVALID
        assert UNKNOWN_CODE_MESSAGE in row.errors
        assert ClientCompany.objects.count() == 0


def test_import_accepts_niteroi_and_empty_municipality_code() -> None:
    firm = make_firm("municipio-import-accept")
    with tenant_context(firm.tenant.id):
        for index, code in enumerate((NITEROI, "")):
            upload = SimpleUploadedFile(
                f"clientes-{index}.csv",
                (
                    "CNPJ;Razão social;UF;IBGE\n"
                    f"{make_cnpj(client_base(index + 3, 'municipio-import-accept'))};"
                    f"Cliente {index};RJ;{code}\n"
                ).encode(),
                content_type="text/csv",
            )
            plan = plan_import(firm.owner, upload, None)
            assert (plan.created, plan.invalid) == (1, 0)
            _ = commit_import(firm.owner, plan.batch_id, expected_version=plan.version)

        imported = list(
            ClientCompany.objects.filter(legal_name__startswith="Cliente ").order_by(
                "legal_name",
            ),
        )

    assert [client.municipality_ibge_code for client in imported] == [NITEROI, ""]


def test_import_rechecks_a_municipality_removed_after_the_preview() -> None:
    firm = make_firm("municipio-stale-preview")
    with tenant_context(firm.tenant.id):
        plan = plan_import(firm.owner, _csv_upload(NITEROI), None)
        assert (plan.created, plan.invalid) == (1, 0)
        Municipality.objects.filter(pk=NITEROI).delete()

        result = commit_import(firm.owner, plan.batch_id, expected_version=plan.version)

        assert (result.created, result.failed) == (0, 1)
        assert not ClientCompany.objects.exists()
        row = ImportRow.objects.get(batch_id=plan.batch_id)
        assert row.client_id is None
        assert row.commit_error


@pytest.mark.parametrize(
    "code", ["3399999", "33A3302", "330330", "33033020", "03303302", "339.9999"]
)
def test_malformed_or_unknown_codes_are_refused_at_both_boundaries(code: str) -> None:
    form = ClientCreateForm(data=_form_data(ClientCreateForm, code))
    assert not form.is_valid()
    errors = form.errors["municipality_ibge_code"]
    if code == UNKNOWN_MUNICIPALITY:
        assert errors == [UNKNOWN_CODE_MESSAGE]
    else:
        assert UNKNOWN_CODE_MESSAGE not in errors
    firm = make_firm("municipio-malformed")
    with tenant_context(firm.tenant.id):
        plan = plan_import(firm.owner, _csv_upload(code), None)
        assert (plan.created, plan.invalid) == (0, 1)
        assert ClientCompany.objects.count() == 0


def test_surrounding_spaces_are_normalized_at_both_boundaries() -> None:
    form = ClientCreateForm(data=_form_data(ClientCreateForm, f" {NITEROI} "))
    assert form.is_valid(), form.errors
    assert form.cleaned_data["municipality_ibge_code"] == NITEROI
    firm = make_firm("municipio-spaces")
    with tenant_context(firm.tenant.id):
        plan = plan_import(firm.owner, _csv_upload(f" {NITEROI} "), None)
        assert (plan.created, plan.invalid) == (1, 0)


@pytest.mark.parametrize("masked", ["330 3302", "3303.302", "33033-02"])
def test_import_keeps_normalizing_masked_codes_before_the_registry_check(
    masked: str,
) -> None:
    firm = make_firm("municipio-masked")
    with tenant_context(firm.tenant.id):
        plan = plan_import(firm.owner, _csv_upload(masked), None)
        row = ImportRow.objects.get(batch_id=plan.batch_id)

        assert (plan.created, plan.invalid) == (1, 0)
        assert row.values["municipality_ibge_code"] == NITEROI


def test_workspace_header_displays_the_registered_municipality() -> None:
    firm = make_firm("municipio-workspace", client_count=1)
    firm.tenant.features = ["cycles_r1"]
    firm.tenant.save(update_fields=["features"])
    client = firm.clients[0]
    with tenant_context(firm.tenant.id):
        ClientCompany.objects.filter(pk=client.pk).update(
            municipality_ibge_code=NITEROI,
        )

    response = firm.as_owner().get(reverse("client-detail", args=[client.pk]))

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    header = body.split("<main", 1)[1].split("<header", 1)[1].split("</header>", 1)[0]
    assert "Niterói/RJ" in header
