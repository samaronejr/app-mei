"""The statutory rights channel, and the guard that keeps PII out of the audit trail.

The PII guard is the load-bearing part. `audit_event` and `audit_platformevent` reject
`UPDATE` and `DELETE` for every role including the table owner, so anything written
into them is permanent. LGPD art. 18 grants erasure rights. A CPF in `metadata` would
therefore be an unerasable store of exactly the data the law grants rights over —
which is why events carry `object_type` and `object_id` and nothing more.
"""

import re
from collections.abc import Iterable
from http import HTTPStatus

import pytest
from django.conf import settings
from django.core import mail
from django.test import Client
from pytest_django.fixtures import SettingsWrapper

from apps.audit.models import (
    AuditAction,
    DataSubjectRelationship,
    DataSubjectRequest,
    DataSubjectRequestType,
    Event,
    PlatformEvent,
)
from apps.audit.services import ObjectRef, record_event
from apps.core.rls import is_exempt_from_tenant_policy
from apps.core.tenancy import tenant_context
from apps.tenants.models import Tenant

pytestmark = pytest.mark.django_db(transaction=True)

DSR_URL = "/lgpd/solicitacao/"

# A CPF is 11 digits, optionally punctuated. A CNPJ is 14 characters — alphanumeric
# from July 2026 — with a two-digit check suffix. Both patterns are deliberately
# tight enough not to match a UUID fragment or a timestamp.
CPF_SHAPE = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
CNPJ_SHAPE = re.compile(r"\b[A-Z0-9]{2}\.[A-Z0-9]{3}\.[A-Z0-9]{3}/[A-Z0-9]{4}-\d{2}\b")


@pytest.fixture(autouse=True)
def _urls(settings: SettingsWrapper) -> None:
    settings.ROOT_URLCONF = "tests.accounts.urls"
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]


@pytest.fixture
def tenant() -> Tenant:
    return Tenant.objects.create(name="Alpha", slug="alpha")


VALID_REQUEST = {
    "requester_name": "Maria Souza",
    "cpf": "529.982.247-25",
    "email": "maria@exemplo.example",
    "relationship": DataSubjectRelationship.HOLDER,
    "request_type": DataSubjectRequestType.DELETION,
    "detail": "Solicito a exclusão dos meus dados.",
}


def test_a_valid_submission_creates_one_request_and_sends_one_email() -> None:
    # Given an anonymous data subject — art. 18 rights belong to the subject, not to
    # an account holder, and most of the people processed here have no login at all
    client = Client()

    # When they submit the form
    response = client.post(DSR_URL, VALID_REQUEST)

    # Then exactly one request is recorded and the encarregado is notified once
    assert response.status_code == HTTPStatus.FOUND
    assert DataSubjectRequest.objects.count() == 1
    assert len(mail.outbox) == 1
    assert settings.LGPD_ENCARREGADO_EMAIL in mail.outbox[0].to

    dsr = DataSubjectRequest.objects.get()
    assert dsr.request_type == DataSubjectRequestType.DELETION
    assert dsr.cpf == VALID_REQUEST["cpf"]


def test_the_submission_is_recorded_as_a_platform_event_without_the_cpf() -> None:
    # Given a submitted request
    Client().post(DSR_URL, VALID_REQUEST)

    # When the audit record is read
    event = PlatformEvent.objects.get(action=AuditAction.DSR_SUBMITTED)

    # Then it names the request, not the document number. The audit trail is
    # append-only; a CPF written there could never be erased.
    dsr = DataSubjectRequest.objects.get()
    assert event.metadata["request_id"] == str(dsr.pk)
    assert VALID_REQUEST["cpf"] not in str(event.metadata)
    assert VALID_REQUEST["cpf"] not in event.subject


def test_an_incomplete_submission_is_rejected_without_creating_a_row() -> None:
    # Given a submission missing the CPF
    incomplete = {**VALID_REQUEST, "cpf": ""}

    # When it is posted
    response = Client().post(DSR_URL, incomplete)

    # Then nothing is recorded and no mail is sent
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert DataSubjectRequest.objects.count() == 0
    assert mail.outbox == []


def test_the_intake_route_is_reachable_without_authentication() -> None:
    # Given no session at all
    # When the form is requested
    response = Client().get(DSR_URL)

    # Then it is served. Gating it would put an access wall in front of a statutory
    # obligation owed to people who have no account here.
    assert response.status_code == HTTPStatus.OK


def test_the_request_table_is_allow_listed_and_not_append_only() -> None:
    # Given the row-level-security allow-list
    # When the table is classified
    assert is_exempt_from_tenant_policy("audit_datasubjectrequest")

    # Then, unlike the audit tables, its rows can be erased — which is the point, as
    # it holds a CPF and the subject may later ask for it to be removed
    Client().post(DSR_URL, VALID_REQUEST)
    assert DataSubjectRequest.objects.count() == 1
    DataSubjectRequest.objects.all().delete()
    assert DataSubjectRequest.objects.count() == 0


def _values_of(metadatas: Iterable[dict[str, object]]) -> list[str]:
    return [str(value) for metadata in metadatas for value in metadata.values()]


def _platform_metadata_values() -> list[str]:
    return _values_of(PlatformEvent.objects.values_list("metadata", flat=True))


def _tenant_metadata_values() -> list[str]:
    """Collect Event metadata for every tenant, one tenant's context at a time.

    Read outside a tenant context this returns NOTHING: audit_event is RLS-forced and
    app_runtime holds no bypass, so the fail-closed policy hides every row and the
    scan would silently examine an empty set. `all_objects` does not help — it skips
    the ORM's ContextVar filter, not the database policy. Entering each tenant's
    context in turn is the only way this half of the guard sees anything at all.
    """
    values: list[str] = []
    for tenant in Tenant.objects.all():  # PLATFORM_QUERY_OK: test-only integrity scan
        with tenant_context(tenant.pk):
            values.extend(
                _values_of(Event.all_objects.values_list("metadata", flat=True)),
            )
    return values


def _document_shaped(values: list[str]) -> list[str]:
    return [v for v in values if CPF_SHAPE.search(v) or CNPJ_SHAPE.search(v)]


def test_the_pii_shape_patterns_actually_match_documents() -> None:
    # Given known document numbers and known non-documents
    # When the patterns are applied
    # Then they match the former and not the latter. Without this control the guard
    # below would pass against a regex that matches nothing at all.
    assert _document_shaped(["529.982.247-25"])
    assert _document_shaped(["52998224725"])
    assert _document_shaped(["12.ABC.345/01DE-35"])
    assert not _document_shaped(["ClientCompany", "owner", "invite_issued"])
    assert not _document_shaped(["0198f3c1-6a2b-7c4d-8e9f-0a1b2c3d4e5f"])


def test_no_audit_metadata_value_is_document_shaped(tenant: Tenant) -> None:
    # Given audit rows written through the product's real paths
    Client().post(DSR_URL, VALID_REQUEST)
    with tenant_context(tenant.id):
        record_event(
            action=AuditAction.EXPORT,
            obj=ObjectRef(type="ClientCompany", id=str(tenant.pk)),
            metadata={"format": "csv", "rows": 12},
        )
    platform_values = _platform_metadata_values()
    tenant_values = _tenant_metadata_values()

    # Both halves must actually hold something. Asserting them separately is the point:
    # the tenant half reads through a fail-closed policy and returns an empty set
    # unless the scan enters each tenant's context, and a combined check would be
    # satisfied by the platform half alone while the Event table went unexamined.
    assert platform_values, "no PlatformEvent metadata — that half would be vacuous"
    assert tenant_values, "no Event metadata visible — that half would be vacuous"

    # When every metadata value in both append-only tables is scanned
    offenders = _document_shaped([*platform_values, *tenant_values])

    # Then none is CPF- or CNPJ-shaped. These tables reject UPDATE and DELETE for
    # every role including their owner, so a document number here is permanent.
    assert not offenders, f"document numbers in append-only audit metadata: {offenders}"
