"""Invariant 2: the same CNPJ is a different company in every firm.

A client registry keyed globally on CNPJ would either refuse the second firm its
own client or — worse — merge the two. This file proves the registry, the search
surface, and the CSV export all treat the CNPJ as a per-firm value: both firms
keep the same number on file, search answers only the caller's firm, and export
writes only the caller's firm.
"""

import csv
import io
import uuid

import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse

from apps.clients.models import ClientCompany
from apps.core.search import search
from apps.core.tenancy import tenant_context
from tests.isolation.axorim_seed import (
    SeedContext,
    make_seed_context,
    signed_in_client,
)
from tests.isolation.rolecheck import assert_isolated_role
from tests.support import enrol_totp

pytestmark = pytest.mark.django_db(transaction=True)

CNPJ = "11222333000181"


def _firms() -> tuple[SeedContext, SeedContext]:
    alpha = make_seed_context("cnpj-alpha", "a", cnpj=CNPJ)
    beta = make_seed_context("cnpj-beta", "b", cnpj=CNPJ)
    return alpha, beta


def test_the_same_cnpj_stores_once_per_firm() -> None:
    # Given the same CNPJ filed in two firms
    alpha, beta = _firms()
    assert alpha.client.cnpj == beta.client.cnpj == CNPJ

    # When a THIRD row with the same number is attempted inside alpha
    with (
        tenant_context(alpha.tenant.id),
        pytest.raises(IntegrityError),
        transaction.atomic(),
    ):
        ClientCompany.all_objects.create(
            tenant=alpha.tenant,
            legal_name="DUPLICATE",
            cnpj=CNPJ,
            is_mei=True,
        )

    # Then the per-tenant unique holds — the boundary is per firm, never global,
    # and never absent. The count must run INSIDE the context: all_objects is the
    # ContextVar-free manager, so outside it the empty GUC hides alpha's own row.
    with tenant_context(alpha.tenant.id):
        assert (
            ClientCompany.all_objects.filter(tenant=alpha.tenant, cnpj=CNPJ).count()
            == 1
        )


def test_search_returns_only_the_callers_firm() -> None:
    # Given the same CNPJ in both firms
    alpha, beta = _firms()
    with tenant_context(alpha.tenant.id):
        hits = list(search(alpha.user, CNPJ))

    # When firm A searches it — RLS confines the queryset, so firm B's row cannot
    # appear even though the number is identical
    keys = {str(hit.id) for hit in hits}
    assert str(beta.client.pk) not in keys
    # And the match that does come back is firm A's own client
    assert str(alpha.client.pk) in keys


def test_export_writes_only_the_callers_firm() -> None:
    # Given the same CNPJ in both firms, and firm A's signed-in member
    alpha, beta = _firms()
    enrol_totp(alpha.user)
    client = signed_in_client(alpha.user, f"{alpha.tenant.slug}.localhost")

    # When the CSV export runs
    response = client.get(reverse("clients-export-csv"))

    # Then it lists alpha's client and never beta's — same CNPJ, different row
    assert response.status_code == 200
    body = response.content.decode(errors="replace")
    assert alpha.client.legal_name in body
    assert beta.client.legal_name not in body
    assert str(beta.client.pk) not in body

    rows = list(csv.reader(io.StringIO(body)))
    cnpj_cells = [cell for row in rows for cell in row if cell == CNPJ]
    assert len(cnpj_cells) == 1, (
        f"expected exactly one row carrying the shared CNPJ, got {len(cnpj_cells)}"
    )


def test_cross_tenant_rows_are_invisible_to_the_unscoped_manager() -> None:
    # Given the same CNPJ in both firms
    alpha, beta = _firms()

    # When alpha's context reads by CNPJ through the UNSCOPED manager — the ORM
    # ContextVar never carries this assertion. The set is materialised inside the
    # block: outside it the restored GUC would hide alpha's row too.
    with tenant_context(alpha.tenant.id):
        visible = {str(row.pk) for row in ClientCompany.all_objects.filter(cnpj=CNPJ)}

    # Then exactly alpha's row comes back; beta's identical number stays hidden
    assert visible == {str(alpha.client.pk)}
    assert str(beta.client.pk) not in visible
    assert_isolated_role()


def test_a_third_firms_object_ids_never_resolve() -> None:
    # Given both firms
    alpha, beta = _firms()
    enrol_totp(alpha.user)
    client = signed_in_client(alpha.user, f"{alpha.tenant.slug}.localhost")

    # When firm A's member requests firm B's client directly
    response = client.get(reverse("client-detail", kwargs={"pk": beta.client.pk}))

    # Then the object is not found — identical CNPJ or not, the row is another
    # firm's and the view answers as though it never existed
    assert response.status_code in {403, 404}
    assert beta.client.legal_name not in response.content.decode(errors="replace")
    unused = uuid.uuid4()
    assert (
        client.get(reverse("client-detail", kwargs={"pk": unused})).status_code == 404
    )
