"""Deterministic HTTP input for the pre-briefing dashboard byte references."""

from datetime import date
from uuid import UUID

import pytest
from django.urls import reverse

from apps.clients.models import ClientCompany, ClientStatus, TaxProfile, TaxRegime
from apps.core.tenancy import tenant_context
from apps.obligations.generator import generate_das_calendar
from tests.ui.factories import make_firm

LEGACY_TODAY = date(2026, 10, 7)
CLIENT_ID = UUID("01900000-0000-7000-8000-000000000019")


def legacy_response(monkeypatch: pytest.MonkeyPatch, *, product_shell: bool) -> bytes:
    monkeypatch.setattr("django.middleware.csrf._get_new_csrf_string", lambda: "a" * 32)
    monkeypatch.setattr(
        "apps.obligations.queries._today", lambda today: today or LEGACY_TODAY
    )
    firm = make_firm("t19-byte-baseline")
    firm.tenant.features = ["cycles_r1"] if product_shell else []
    firm.tenant.save(update_fields=["features"])
    with tenant_context(firm.tenant.pk):
        company = ClientCompany.objects.create(
            id=CLIENT_ID,
            tenant=firm.tenant,
            legal_name="Cliente de referência",
            trade_name="Referência",
            cnpj="11222333000181",
            status=ClientStatus.ACTIVE,
            opened_on=date(2020, 3, 10),
        )
        TaxProfile.objects.create(
            tenant=firm.tenant,
            client=company,
            regime=TaxRegime.MEI,
            valid_from=date(2020, 3, 10),
            provenance="staff_entry",
        )
        generate_das_calendar(company, starting_from=date(2026, 8, 1))
    response = firm.as_owner().get(reverse("dashboard"))
    assert response.status_code == 200
    assert not response.streaming
    return response.content
