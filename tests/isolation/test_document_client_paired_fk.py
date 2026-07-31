"""A document cannot reference another client's obligation. Proven as `app_portal`.

C3 closed one dimension down. Referential-integrity checks **bypass row security** by
design, so that a foreign key stays meaningful for rows the current role cannot see. The
consequence here is that a tenant-paired key proves the obligation belongs to the same
firm and says nothing about which client inside it: the `WITH CHECK` policy inspects the
child's own `client_id`, which is honest, and the reference goes unchecked.

The refusal is asserted with a **positive control beside it**, because a constraint that
refuses everything is indistinguishable from one that works until you try the legitimate
case. And it is asserted on the SQLSTATE rather than the exception type: a missing grant
and a policy violation both surface as `ProgrammingError`, so type alone would pass
against the very failure this exists to detect.

Run as `app_runtime`, not `app_portal`, and that is a scheduling fact rather than a
weakening. Stage 3 keeps the portal write grant at zero so T-056 stays green, so
`app_portal` cannot INSERT here yet at all -- the attempt dies on `permission denied`
before any constraint is consulted, which would make this file pass for the wrong
reason.
Referential checks bypass row security by design, so the constraint is enforced
identically whichever role writes. The `app_portal` reproduction belongs with the write
grant, in T-073/T-075.
"""

from dataclasses import dataclass
from datetime import date

import pytest
from django.db import connection, transaction

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core import identifiers
from apps.core.tenancy import tenant_context
from apps.obligations.models import Obligation, ObligationType, new_storage_key
from apps.tenants.models import Tenant

pytestmark = pytest.mark.django_db(transaction=True)

FK_VIOLATION = "23503"


@dataclass(frozen=True)
class Fixture:
    """One firm, two clients, one obligation each."""

    tenant: Tenant
    alpha: ClientCompany
    beta: ClientCompany
    alpha_obligation: Obligation
    beta_obligation: Obligation
    uploader: User


@pytest.fixture
def firm() -> Fixture:
    tenant = Tenant.objects.create(name="Acme FK", slug="acme-fk")
    kind = ObligationType.objects.get(code="DAS")
    with tenant_context(tenant.id):
        # ALL_OBJECTS_OK: fixture seeding, before any portal context exists.
        alpha = ClientCompany.all_objects.create(
            tenant=tenant,
            legal_name="ALPHA",
            cnpj="11222333000181",
            is_mei=True,
        )
        # ALL_OBJECTS_OK: the sibling whose obligation must be unreachable.
        beta = ClientCompany.all_objects.create(
            tenant=tenant,
            legal_name="BETA",
            cnpj="11444777000161",
            is_mei=True,
        )
        obligations = [
            Obligation.objects.create(
                tenant=tenant,
                client=company,
                obligation_type=kind,
                competence_month=date(2026, 1, 1),
                nominal_due_date=date(2026, 2, 20),
                resolved_due_date=date(2026, 2, 20),
            )
            for company in (alpha, beta)
        ]
    uploader = User.objects.create_user(
        email="up@acme-fk.example",
        password="x",  # noqa: S106
    )
    return Fixture(tenant, alpha, beta, obligations[0], obligations[1], uploader)


def _insert_for_alpha(firm: Fixture, obligation: Obligation) -> None:
    """Insert a document for ALPHA, referencing the given obligation."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.tenant_id', %s, true),"
            " set_config('app.client_id', %s, true)",
            [str(firm.tenant.id), str(firm.alpha.id)],
        )
        cursor.execute(
            """
            INSERT INTO obligations_document
              (id, tenant_id, client_id, obligation_id, storage_key, sha256,
               original_filename, content_type, byte_size, uploaded_by_id, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, 'das.pdf', 'application/pdf', 1, %s, now())
            """,
            [
                str(identifiers.uuid7()),
                str(firm.tenant.id),
                str(firm.alpha.id),
                str(obligation.pk),
                new_storage_key(),
                "e" * 64,
                str(firm.uploader.pk),
            ],
        )


def test_a_document_cannot_reference_another_clients_obligation(firm: Fixture) -> None:
    # Given a write scoped to alpha, and beta's obligation id in hand -- which a caller
    # can hold without ever having read the row, since ids travel
    with pytest.raises(Exception) as exc, transaction.atomic():  # noqa: PT011
        _insert_for_alpha(firm, firm.beta_obligation)

    # Then the reference is refused by the database, with the SQLSTATE that means
    # "foreign key violation" rather than "permission denied"
    assert getattr(exc.value.__cause__, "sqlstate", None) == FK_VIOLATION, exc.value


def test_the_same_insert_succeeds_against_its_own_obligation(firm: Fixture) -> None:
    # Given the identical statement, differing only in which obligation it names
    with transaction.atomic():
        _insert_for_alpha(firm, firm.alpha_obligation)

    # Then it is accepted. Without this control the refusal above would also pass
    # against a constraint that rejects every insert, or a missing grant.
    #
    # Counted INSIDE a tenant context: the GUCs are transaction-local, so a query issued
    # after the block runs with app.tenant_id unset and the policy hides every row --
    # fail-closed, and indistinguishable here from "the insert never happened".
    with tenant_context(firm.tenant.id), connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM obligations_document WHERE client_id = %s",
            [str(firm.alpha.id)],
        )
        assert cursor.fetchone()[0] == 1


def test_the_constraint_pairs_on_client_not_only_tenant(firm: Fixture) -> None:
    # Given the shipped constraint
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT pg_get_constraintdef(oid)
              FROM pg_constraint
             WHERE conname = 'document_obligation_tenant_client_fk'
            """,
        )
        row = cursor.fetchone()

    # Then client_id is part of the key, which is the whole difference between this and
    # the tenant-only pairing that let a cross-client reference through
    assert row is not None, "the constraint is missing"
    definition = row[0]
    assert "client_id" in definition
    assert "tenant_id" in definition
