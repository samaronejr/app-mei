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
from typing import Literal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.urls import reverse

from apps.clients.importing import commit_import, plan_import
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


def _import_client(ctx: SeedContext, cnpj: str, name: str) -> ClientCompany:
    """Write one client through the real plan->commit import path.

    The shared-CNPJ clients must arrive the way production rows do — through
    the importer — not through an ORM seed helper that bypasses the duplicate
    checks, capability gates, and provenance writes the feature actually runs.
    """
    upload = SimpleUploadedFile(
        "clientes.csv",
        f"CNPJ;Razao social;UF\n{cnpj};{name};SP\n".encode(),
        content_type="text/csv",
    )
    with tenant_context(ctx.tenant.id):
        plan = plan_import(ctx.user, upload, None)
        result = commit_import(ctx.user, plan.batch_id, expected_version=plan.version)
        # ALL_OBJECTS_OK: the import-path assertion reads the unscoped manager
        # inside the seeded tenant context.
        rows = list(ClientCompany.all_objects.filter(tenant=ctx.tenant, cnpj=cnpj))
    assert result.created == 1, (
        f"the real import path did not create {name}: {result!r}"
    )
    assert rows, f"no {cnpj} row after the import committed in {ctx.tenant.slug}"
    assert rows[0].import_batch_id == plan.batch_id
    return rows[0]


def _firms() -> tuple[SeedContext, SeedContext]:
    """Two ORM-seeded firms for the per-tenant uniqueness constraint probe."""
    alpha = make_seed_context("cnpj-alpha", "a", cnpj=CNPJ)
    beta = make_seed_context("cnpj-beta", "b", cnpj=CNPJ)
    return alpha, beta


def _imported_firms() -> tuple[SeedContext, SeedContext]:
    """Two firms whose shared-CNPJ clients BOTH arrive via the real importer.

    The seeded context client gets a per-firm CNPJ so the shared number is
    written ONLY by plan_import/commit_import on each side — the row every
    test asserts on is an importer-built row, not an ORM seed helper's.
    """
    # The seeded context client carries a per-firm CNPJ (any valid number the
    # shared one isn't), so plan_import/commit_import writes the shared CNPJ
    # itself on both sides.
    alpha = make_seed_context("cnpj-alpha", "a", cnpj="11444777000161")
    beta = make_seed_context("cnpj-beta", "b", cnpj="11444777000161")
    alpha.client = _import_client(alpha, CNPJ, f"IMPORTADA {alpha.tag.upper()} MARKER")
    beta.client = _import_client(beta, CNPJ, f"IMPORTADA {beta.tag.upper()} MARKER")
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
        # ALL_OBJECTS_OK: duplicate-CNPJ probe inserted inside the alpha tenant context
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
            # ALL_OBJECTS_OK: counting the probe row; unscoped read inside the context.
            ClientCompany.all_objects.filter(tenant=alpha.tenant, cnpj=CNPJ).count()
            == 1
        )


def test_import_writes_once_per_firm() -> None:
    # Given the same CNPJ imported through the real plan->commit path in BOTH
    # firms — each importer-built row is a different record under a different
    # tenant, never merged and never refused cross-firm.
    alpha, beta = _imported_firms()
    assert_isolated_role()
    assert alpha.client.cnpj == beta.client.cnpj == CNPJ
    assert alpha.client.pk != beta.client.pk
    assert alpha.client.tenant.pk == alpha.tenant.pk
    assert beta.client.tenant.pk == beta.tenant.pk

    # And a second import of the SAME CNPJ inside firm A duplicates, not
    # doubles — the per-tenant duplicate check still bites on the same side.
    with tenant_context(alpha.tenant.id):
        plan2 = plan_import(
            alpha.user,
            SimpleUploadedFile(
                "clientes.csv",
                f"CNPJ;Razao social;UF\n{CNPJ};Duplicata;SP\n".encode(),
                content_type="text/csv",
            ),
            None,
        )
    assert plan2.duplicate == 1


@pytest.mark.parametrize("firm", ["alpha", "beta"])
def test_search_returns_only_the_callers_firm(firm: Literal["alpha", "beta"]) -> None:
    # Given the same CNPJ imported through the real importer in both firms
    firms = dict(zip(("alpha", "beta"), _imported_firms(), strict=True))
    alpha = firms[firm]
    beta = firms["beta" if firm == "alpha" else "alpha"]
    with tenant_context(alpha.tenant.id):
        hits = list(search(alpha.user, CNPJ))

    # When firm A searches it — RLS confines the queryset, so firm B's row cannot
    # appear even though the number is identical
    keys = {str(hit.id) for hit in hits}
    assert str(beta.client.pk) not in keys
    # And the match that does come back is firm A's own client
    assert str(alpha.client.pk) in keys


@pytest.mark.parametrize("firm", ["alpha", "beta"])
def test_export_writes_only_the_callers_firm(firm: Literal["alpha", "beta"]) -> None:
    # Given the same CNPJ imported in both firms, and firm A's signed-in member
    firms = dict(zip(("alpha", "beta"), _imported_firms(), strict=True))
    alpha = firms[firm]
    beta = firms["beta" if firm == "alpha" else "alpha"]
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
    # Given the same CNPJ imported in both firms
    alpha, beta = _imported_firms()

    # When alpha's context reads by CNPJ through the UNSCOPED manager — the ORM
    # ContextVar never carries this assertion. The set is materialised inside the
    # block: outside it the restored GUC would hide alpha's row too.
    with tenant_context(alpha.tenant.id):
        # ALL_OBJECTS_OK: the assertion reads the unscoped manager on purpose.
        visible = {str(row.pk) for row in ClientCompany.all_objects.filter(cnpj=CNPJ)}

    # Then exactly alpha's row comes back; beta's identical number stays hidden
    assert visible == {str(alpha.client.pk)}
    assert str(beta.client.pk) not in visible
    assert_isolated_role()


def test_a_third_firms_object_ids_never_resolve() -> None:
    # Given both firms' importer-built shared-CNPJ clients
    alpha, beta = _imported_firms()
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
