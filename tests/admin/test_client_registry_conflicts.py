"""Admin requests must authorize and lock before either side of the dual-write."""

from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest
from django.contrib.auth.models import Permission
from django.db import connection, connections
from django.test import Client
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import Event
from apps.clients.admin import ClientCompanyForm
from apps.clients.models import ClientCompany, TaxProfile, TaxRegime
from apps.clients.services import correct_tax_profile
from apps.core.tenancy import tenant_context
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.admin import test_client_tax_profile_dual_write as dual_write
from tests.admin.test_client_tax_profile_dual_write import (
    ADD_URL,
    CHANGE_URL,
    SELECT_URL,
    VALID_CNPJ,
    _base_form,
)
from tests.support import enrol_totp

pytestmark = pytest.mark.django_db(transaction=True)
_urls = dual_write._urls
operator_client = dual_write.operator_client


def _snapshot(tenant: Tenant, pk: UUID) -> tuple[object, ...]:
    with tenant_context(tenant.id):
        return (
            list(ClientCompany.objects.filter(pk=pk).values()),
            list(TaxProfile.objects.order_by("pk").values()),
            list(Event.objects.order_by("pk").values()),
        )


def _competing_correction(tenant: Tenant, pk: UUID) -> int:
    try:
        # Threads have distinct connections; use the runtime role just like HTTP.
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE app_runtime")
        with tenant_context(tenant.id):
            client = ClientCompany.objects.get(pk=pk)
            correct_tax_profile(
                actor=User.objects.get(email="op@platform.example"),
                client=client,
                regime=TaxRegime.SIMPLES,
                valid_from=timezone.localdate(),
                expected_revision=client.tax_profile_revision,
            )
            client.refresh_from_db()
            revision = client.tax_profile_revision
        return int(revision)
    finally:
        connections.close_all()


@pytest.mark.parametrize("interleave_post", [False, True])
def test_stale_admin_caller_cannot_overwrite_a_committed_correction(
    operator_client: tuple[Client, Tenant], interleave_post: bool
) -> None:
    http, tenant = operator_client
    assert http.post(ADD_URL, _base_form(is_mei="on")).status_code == HTTPStatus.FOUND
    with tenant_context(tenant.id):
        client = ClientCompany.objects.get(cnpj=VALID_CNPJ)
        revision = client.tax_profile_revision
    url = CHANGE_URL.format(pk=client.pk)
    page = http.get(url)
    assert page.status_code == HTTPStatus.OK
    form = page.context["adminform"].form
    assert form.fields["tax_profile_revision"].widget.is_hidden
    assert form["tax_profile_revision"].value() == revision

    before: tuple[object, ...] = ()

    def compete() -> None:
        nonlocal before
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert (
                pool.submit(_competing_correction, tenant, client.pk).result(15)
                == revision + 1
            )
        before = _snapshot(tenant, client.pk)

    original_clean = ClientCompanyForm.clean

    def clean_with_competitor(self: ClientCompanyForm) -> dict[str, Any] | None:
        compete()
        return original_clean(self)

    data = _base_form(
        is_mei="on", legal_name="Stale rename", tax_profile_revision=revision
    )
    if interleave_post:
        with patch.object(ClientCompanyForm, "clean", clean_with_competitor):
            response = http.post(url, data)
    else:
        compete()
        response = http.post(url, data)
    assert response.status_code == HTTPStatus.OK
    errors = response.context["adminform"].form.non_field_errors().as_data()
    assert any(error.code == "stale_tax_profile" for error in errors)
    assert _snapshot(tenant, client.pk) == before


def test_unassigned_admin_cannot_save_a_matching_profile(
    operator_client: tuple[Client, Tenant],
) -> None:
    http, tenant = operator_client
    assert http.post(ADD_URL, _base_form(is_mei="on")).status_code == HTTPStatus.FOUND
    with tenant_context(tenant.id):
        client = ClientCompany.objects.get(cnpj=VALID_CNPJ)
        ClientCompany.objects.filter(pk=client.pk).update(is_mei=False)
    staff = User.objects.create_user(email="unassigned@example.test", is_staff=True)
    staff.user_permissions.add(
        *Permission.objects.filter(
            content_type__app_label="clients",
            codename__in=("view_clientcompany", "change_clientcompany"),
        )
    )
    enrol_totp(staff)
    Membership.objects.create(
        user=staff, tenant=tenant, role=TenantRole.STAFF_ACCOUNTANT
    )
    http.force_login(staff)
    assert (
        http.post(SELECT_URL, {"tenant": str(tenant.pk), "next": ADD_URL}).status_code
        == HTTPStatus.FOUND
    )
    before = _snapshot(tenant, client.pk)
    response = http.post(
        CHANGE_URL.format(pk=client.pk),
        _base_form(
            is_mei="on",
            legal_name="Forbidden rename",
            tax_profile_revision=client.tax_profile_revision,
        ),
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert _snapshot(tenant, client.pk) == before


@pytest.mark.parametrize("revision", [None, "invalid", 0])
def test_invalid_revision_never_falls_back_to_the_current_snapshot(
    operator_client: tuple[Client, Tenant], revision: object
) -> None:
    http, tenant = operator_client
    assert http.post(ADD_URL, _base_form(is_mei="on")).status_code == HTTPStatus.FOUND
    with tenant_context(tenant.id):
        client = ClientCompany.objects.get(cnpj=VALID_CNPJ)
    before = _snapshot(tenant, client.pk)
    data = _base_form(is_mei="on", legal_name="Invalid revision rename")
    if revision is not None:
        data["tax_profile_revision"] = revision
    response = http.post(CHANGE_URL.format(pk=client.pk), data)
    assert response.status_code == HTTPStatus.OK
    assert "tax_profile_revision" in response.context["adminform"].form.errors
    assert _snapshot(tenant, client.pk) == before
