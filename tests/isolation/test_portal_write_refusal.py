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

import hashlib
from dataclasses import dataclass
from datetime import date

import pytest
from django.db import connection, transaction
from django.db.backends.utils import CursorWrapper
from psycopg import sql

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core import identifiers
from apps.core.tenancy import tenant_context
from apps.cycles.models import EvidenceRequest
from apps.obligations.models import (
    Document,
    Obligation,
    ObligationType,
    new_storage_key,
)
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


# ------------------------------------------------ T11: the upload-against-request rows
#
# The portal INSERTs an evidence link, the intake row of a new document and one
# submission event. Each table carries the RESTRICTIVE client-anchored WITH CHECK, and
# the request/document/cycle references are client-paired composite keys. Each refusal
# below asserts the NAMED policy (or the FK sqlstate) and the absence of "permission
# denied", with the honest write beside it as the positive control.

T11_TABLES = ("cycles_evidencelink", "cycles_documentintake", "cycles_cycleevent")


@dataclass(frozen=True)
class RequestGraph:
    """Two clients, each with an open request and a stored document."""

    tenant: Tenant
    alpha: ClientCompany
    beta: ClientCompany
    alpha_request: EvidenceRequest
    beta_request: EvidenceRequest
    alpha_document: Document
    beta_document: Document
    uploader: User


@pytest.fixture
def requests_graph() -> RequestGraph:
    from tests.portal.request_fixtures import portal_setup  # noqa: PLC0415

    setup = portal_setup("w1-requests")
    documents = []
    with tenant_context(setup.graph.tenant.pk):
        for company, name in (
            (setup.graph.client, "alpha"),
            (setup.other_client, "beta"),
        ):
            documents.append(
                Document.objects.create(
                    tenant=setup.graph.tenant,
                    client=company,
                    sha256=hashlib.sha256(name.encode()).hexdigest(),
                    original_filename=f"{name}.pdf",
                    content_type="application/pdf",
                    byte_size=1,
                    uploaded_by=setup.portal_user,
                )
            )
    return RequestGraph(
        setup.graph.tenant,
        setup.graph.client,
        setup.other_client,
        setup.request,
        setup.other_request,
        documents[0],
        documents[1],
        setup.portal_user,
    )


def _as_portal(graph: RequestGraph, confined_to: ClientCompany) -> CursorWrapper:
    cursor = connection.cursor()
    cursor.execute(f"SET LOCAL ROLE {PORTAL_ROLE}")
    cursor.execute(
        "SELECT set_config('app.tenant_id', %s, true),"
        " set_config('app.client_id', %s, true)",
        [str(graph.tenant.id), str(confined_to.id)],
    )
    return cursor


def _t11_insert(  # noqa: PLR0913 - keyword-only: each is an axis under test
    graph: RequestGraph,
    table: str,
    *,
    confined_to: ClientCompany,
    attributed_to: ClientCompany,
    request: EvidenceRequest,
    document: Document,
) -> None:
    cursor = _as_portal(graph, confined_to)
    row_id = str(identifiers.uuid7())
    if table == "cycles_evidencelink":
        cursor.execute(
            "INSERT INTO cycles_evidencelink (id, tenant_id, client_id, request_id,"
            " document_id, origin, relinked, submitted_by_id, created_at)"
            " VALUES (%s, %s, %s, %s, %s, 'portal', false, %s, now())",
            [
                row_id,
                str(graph.tenant.id),
                str(attributed_to.id),
                str(request.pk),
                str(document.pk),
                str(graph.uploader.pk),
            ],
        )
    elif table == "cycles_documentintake":
        cursor.execute(
            "INSERT INTO cycles_documentintake (id, tenant_id, client_id,"
            " document_id, state, reason, created_at)"
            " VALUES (%s, %s, %s, %s, 'recebido', '', now())",
            [row_id, str(graph.tenant.id), str(attributed_to.id), str(document.pk)],
        )
    else:
        cursor.execute(
            "INSERT INTO cycles_cycleevent (id, tenant_id, client_id, cycle_id, type,"
            " schema_version, actor_id, aggregate_type, aggregate_id,"
            " aggregate_version, occurred_at, observed_at, correlation_id, payload)"
            " VALUES (%s, %s, %s, %s, 'evidence.submitted', 1, %s, 'EvidenceRequest',"
            " %s, 1, now(), now(), %s, '{}'::jsonb)",
            [
                row_id,
                str(graph.tenant.id),
                str(attributed_to.id),
                str(request.cycle_id),
                str(graph.uploader.pk),
                str(request.pk),
                str(identifiers.uuid7()),
            ],
        )


def _count(graph: RequestGraph, table: str, client: ClientCompany) -> int:
    with tenant_context(graph.tenant.id), connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("SELECT count(*) FROM {} WHERE client_id = %s").format(
                sql.Identifier(table)
            ),
            [str(client.id)],
        )
        row = cursor.fetchone()
    return int(row[0]) if row else 0


@pytest.mark.parametrize("table", T11_TABLES)
def test_t11_a_forged_client_id_is_refused_by_the_named_policy(
    requests_graph: RequestGraph, table: str
) -> None:
    graph = requests_graph
    before = _count(graph, table, graph.beta)
    with pytest.raises(Exception) as exc, transaction.atomic():  # noqa: PT011
        _t11_insert(
            graph,
            table,
            confined_to=graph.alpha,
            attributed_to=graph.beta,
            request=graph.beta_request,
            document=graph.beta_document,
        )
    message = str(exc.value)
    assert RLS_VIOLATION in message, message
    assert f"{table}_portal_client_isolation" in message, message
    assert PERMISSION_DENIED not in message, message
    assert _count(graph, table, graph.beta) == before


@pytest.mark.parametrize("table", T11_TABLES)
def test_t11_the_positive_control_writes_its_own_client(
    requests_graph: RequestGraph, table: str
) -> None:
    graph = requests_graph
    before = _count(graph, table, graph.alpha)
    with transaction.atomic():
        _t11_insert(
            graph,
            table,
            confined_to=graph.alpha,
            attributed_to=graph.alpha,
            request=graph.alpha_request,
            document=graph.alpha_document,
        )
    assert _count(graph, table, graph.alpha) == before + 1


@pytest.mark.parametrize(
    ("table", "reference"),
    [
        ("cycles_evidencelink", "request"),
        ("cycles_evidencelink", "document"),
        ("cycles_documentintake", "document"),
    ],
)
def test_t11_an_honest_row_naming_a_foreign_parent_is_refused_by_the_fk(
    requests_graph: RequestGraph, table: str, reference: str
) -> None:
    graph = requests_graph
    with pytest.raises(Exception) as exc, transaction.atomic():  # noqa: PT011
        _t11_insert(
            graph,
            table,
            confined_to=graph.alpha,
            attributed_to=graph.alpha,
            request=graph.beta_request
            if reference == "request"
            else graph.alpha_request,
            document=graph.beta_document
            if reference == "document"
            else graph.alpha_document,
        )
    assert getattr(exc.value.__cause__, "sqlstate", None) == FK_VIOLATION, exc.value
    assert PERMISSION_DENIED not in str(exc.value)


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM cycles_cycleevent",
        "SELECT * FROM cycles_requestinternal",
        "SELECT * FROM cycles_internalnote",
        "UPDATE cycles_evidencelink SET relinked = true",
        "DELETE FROM cycles_evidencelink",
        "UPDATE cycles_documentintake SET state = 'aceito'",
        "UPDATE cycles_evidencerequest SET state = 'aceito'",
        "INSERT INTO cycles_requestinternal (id) VALUES (gen_random_uuid())",
    ],
)
def test_t11_the_portal_cannot_read_internals_or_rewrite_submissions(
    requests_graph: RequestGraph, statement: str
) -> None:
    with pytest.raises(Exception) as exc, transaction.atomic():  # noqa: PT011
        _as_portal(requests_graph, requests_graph.alpha).execute(statement)
    assert PERMISSION_DENIED in str(exc.value), exc.value
