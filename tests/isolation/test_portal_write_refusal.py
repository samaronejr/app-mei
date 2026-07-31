"""What refuses a portal write, proven as `app_portal` now that INSERT exists.

Until Stage 4 the portal held no write privilege anywhere, so every attempted INSERT
died on `permission denied` before a policy was consulted. That made the six
client-scoped `WITH CHECK` clauses dead code — present, correct, and never once
exercised. Widening the grant animates them, and this is where they first fire.

**The message is asserted, not the exception type.** A revoked grant and a policy
violation are both SQLSTATE `42501` and both surface as `ProgrammingError`, so asserting
the type would pass against the very failure this detects: someone revoking the grant
instead of fixing the policy, and seeing green. Each refusal asserts the policy is
*named* and that `permission denied` is *absent*.

Every refusal has a positive control beside it. A policy that refuses everything is
indistinguishable from one that works until the legitimate write is tried.
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

PORTAL_ROLE = "app_portal"
RLS_VIOLATION = "violates row-level security policy"
PERMISSION_DENIED = "permission denied"
POLICY_NAME = "obligations_document_portal_client_isolation"
FK_VIOLATION = "23503"


@dataclass(frozen=True)
class Fixture:
    """One firm, two clients, an obligation each."""

    tenant: Tenant
    alpha: ClientCompany
    beta: ClientCompany
    alpha_obligation: Obligation
    beta_obligation: Obligation
    uploader: User


@pytest.fixture
def firm() -> Fixture:
    tenant = Tenant.objects.create(name="Acme W1", slug="acme-w1")
    kind = ObligationType.objects.get(code="DAS")
    with tenant_context(tenant.id):
        # ALL_OBJECTS_OK: fixture seeding, before any portal context exists.
        alpha = ClientCompany.all_objects.create(
            tenant=tenant,
            legal_name="ALPHA",
            cnpj="11222333000181",
            is_mei=True,
        )
        # ALL_OBJECTS_OK: the sibling the portal must never write as.
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
        email="up@acme-w1.example",
        password="x",  # noqa: S106
    )
    return Fixture(tenant, alpha, beta, obligations[0], obligations[1], uploader)


def _insert_as_portal(
    firm: Fixture,
    *,
    confined_to: ClientCompany,
    attributed_to: ClientCompany,
    obligation: Obligation | None = None,
) -> None:
    """INSERT a document as `app_portal`, confined to one client, claiming another."""
    with connection.cursor() as cursor:
        cursor.execute(f"SET LOCAL ROLE {PORTAL_ROLE}")
        cursor.execute(
            "SELECT set_config('app.tenant_id', %s, true),"
            " set_config('app.client_id', %s, true)",
            [str(firm.tenant.id), str(confined_to.id)],
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
                str(attributed_to.id),
                str(obligation.pk) if obligation else None,
                new_storage_key(),
                "a" * 64,
                str(firm.uploader.pk),
            ],
        )


def test_w1_a_forged_client_id_is_refused_by_the_named_policy(firm: Fixture) -> None:
    # Given a portal session confined to alpha, writing a row claiming to be beta's
    with pytest.raises(Exception) as exc, transaction.atomic():  # noqa: PT011
        _insert_as_portal(firm, confined_to=firm.alpha, attributed_to=firm.beta)

    message = str(exc.value)

    # Then the ROW SECURITY POLICY refuses it, by name
    assert RLS_VIOLATION in message, message
    assert POLICY_NAME in message, message

    # And it is NOT a missing grant. This clause is what turns the mutation "revoke the
    # grant instead of forging the id" red: both failures are 42501 and both surface as
    # ProgrammingError, so asserting the type alone would pass against it.
    assert PERMISSION_DENIED not in message, message


def test_w1_the_positive_control_writes_its_own_client(firm: Fixture) -> None:
    # Given the identical statement, differing only in the client it claims
    with transaction.atomic():
        _insert_as_portal(firm, confined_to=firm.alpha, attributed_to=firm.alpha)

    # Then it is accepted. Without this the refusal above would also pass against a
    # policy that rejects every write, or against a grant that was never made.
    with tenant_context(firm.tenant.id), connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM obligations_document WHERE client_id = %s",
            [str(firm.alpha.id)],
        )
        assert cursor.fetchone()[0] == 1


def test_t070_a_cross_client_obligation_is_refused_as_the_portal_role(
    firm: Fixture,
) -> None:
    """T-070's deferred leg, runnable now that the portal can INSERT at all.

    At T-070 time `app_portal` held no write privilege, so this died on `permission
    denied` before the constraint was consulted and would have passed for the wrong
    reason. The reference is refused by the FOREIGN KEY, not the policy: the row's own
    `client_id` is honest, so `WITH CHECK` is satisfied and the composite key catches
    it.
    """
    with pytest.raises(Exception) as exc, transaction.atomic():  # noqa: PT011
        _insert_as_portal(
            firm,
            confined_to=firm.alpha,
            attributed_to=firm.alpha,
            obligation=firm.beta_obligation,
        )

    assert getattr(exc.value.__cause__, "sqlstate", None) == FK_VIOLATION, exc.value
    assert PERMISSION_DENIED not in str(exc.value)


def test_t070_the_positive_control_attaches_its_own_obligation(firm: Fixture) -> None:
    with transaction.atomic():
        _insert_as_portal(
            firm,
            confined_to=firm.alpha,
            attributed_to=firm.alpha,
            obligation=firm.alpha_obligation,
        )

    with tenant_context(firm.tenant.id), connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM obligations_document WHERE obligation_id = %s",
            [str(firm.alpha_obligation.pk)],
        )
        assert cursor.fetchone()[0] == 1
