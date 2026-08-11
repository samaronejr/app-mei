"""The statutory rights channel, and the guard that keeps PII out of the audit trail.

The PII guard is the load-bearing part. `audit_event` and `audit_platformevent` reject
`UPDATE` and `DELETE` for every role including the table owner, so anything written
into them is permanent. LGPD art. 18 grants erasure rights. A CPF in `metadata` would
therefore be an unerasable store of exactly the data the law grants rights over —
which is why events carry `object_type` and `object_id` and nothing more.
"""

import re
from collections.abc import Iterable
from datetime import UTC, datetime
from http import HTTPStatus
from unittest.mock import Mock

import pytest
from django.conf import settings
from django.contrib import admin
from django.core import mail
from django.db import transaction
from django.test import Client, RequestFactory
from pytest_django.fixtures import (
    DjangoCaptureOnCommitCallbacks,
    SettingsWrapper,
)

from apps.audit.admin import DataSubjectRequestAdmin
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
from apps.lgpd.views import data_subject_request_view
from apps.tenants.models import Tenant

pytestmark = pytest.mark.django_db(transaction=True)

DSR_URL = "/lgpd/solicitacao/"

# A CPF is 11 digits. A CNPJ is 14 characters — alphanumeric from July 2026 — with a
# two-digit check suffix.
#
# Every separator is optional in both, because `normalize_document` strips the mask
# before the value reaches the column: the realistic leak this guard exists to catch
# is `metadata={"cnpj": company.cnpj}`, which carries the BARE form. A CNPJ pattern
# requiring the mask could not match anything the database actually holds, so that
# half of the guard scanned real rows and was structurally incapable of finding a
# document in them.
#
# Widening costs nothing in precision: both patterns still demand a full-length run
# bounded by `\b` and ending in two digits, which no UUID (in any casing), timestamp,
# action slug or identifier the audit tables carry can satisfy. The controls below
# assert exactly that, in both directions.
CPF_SHAPE = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
CNPJ_SHAPE = re.compile(
    r"\b[A-Z0-9]{2}\.?[A-Z0-9]{3}\.?[A-Z0-9]{3}/?[A-Z0-9]{4}-?\d{2}\b",
)


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


def test_a_valid_submission_creates_one_request_and_sends_one_email(
    django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an anonymous data subject — art. 18 rights belong to the subject, not to
    # an account holder, and most of the people processed here have no login at all
    client = Client()
    stamped_at = datetime(2026, 8, 10, 12, tzinfo=UTC)
    clock = Mock(return_value=stamped_at)
    monkeypatch.setattr("apps.lgpd.views.now", clock)

    # When they submit the form
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(DSR_URL, VALID_REQUEST)

    # Then exactly one request is recorded and the encarregado is notified once
    assert response.status_code == HTTPStatus.FOUND
    assert DataSubjectRequest.objects.count() == 1
    assert len(mail.outbox) == 1
    assert settings.LGPD_ENCARREGADO_EMAIL in mail.outbox[0].to

    dsr = DataSubjectRequest.objects.get()
    assert dsr.request_type == DataSubjectRequestType.DELETION
    assert dsr.cpf == VALID_REQUEST["cpf"]
    assert dsr.encarregado_notified_at == stamped_at
    clock.assert_called_once_with()

    message = mail.outbox[0].body
    assert str(dsr.pk) in message
    assert dsr.request_type in message
    assert dsr.relationship in message
    assert dsr.requester_name not in message
    assert dsr.cpf not in message
    assert dsr.email not in message
    assert dsr.detail not in message


def test_a_send_failure_is_durable_and_never_surfaces_from_the_callback(
    django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given the encarregado's mail server is unavailable
    error = RuntimeError("SMTP indisponível")
    unavailable = Mock(side_effect=error)
    captured = Mock()
    monkeypatch.setattr("apps.lgpd.views.send_mail", unavailable)
    monkeypatch.setattr("sentry_sdk.capture_exception", captured)

    # When an anonymous data subject files a legal request
    with django_capture_on_commit_callbacks(execute=True):
        response = Client(raise_request_exception=False).post(
            DSR_URL,
            VALID_REQUEST,
            follow=True,
        )

    # Then the filing survives the delivery failure. Losing this row would silently
    # erase the statutory request merely because a separate notification failed.
    dsr = DataSubjectRequest.objects.get()
    assert dsr.encarregado_notified_at is None
    assert dsr.notification_last_error == str(error)
    assert VALID_REQUEST["cpf"] not in dsr.notification_last_error
    assert "Uma solicitação de titular" not in dsr.notification_last_error
    unavailable.assert_called_once()
    captured.assert_called_once_with(error)
    assert response.status_code == HTTPStatus.OK
    assert "Sua solicitação foi registrada" in response.content.decode()


def test_a_rolled_back_submission_never_runs_its_notification(
    django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a notification registered by an intake transaction
    send = Mock()
    monkeypatch.setattr("apps.lgpd.views.send_mail", send)
    request = RequestFactory().post(DSR_URL, VALID_REQUEST)

    # When later work makes that transaction roll back
    with (
        django_capture_on_commit_callbacks(execute=True),
        transaction.atomic(),
    ):
        data_subject_request_view(request)
        transaction.set_rollback(True)

    # Then commit-dependent work never runs against a row that does not exist
    assert DataSubjectRequest.objects.count() == 0
    send.assert_not_called()


def test_the_submission_is_recorded_as_a_platform_event_without_the_cpf(
    django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks,
) -> None:
    # Given a submitted request
    with django_capture_on_commit_callbacks(execute=True):
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


def test_the_request_table_is_allow_listed_and_not_append_only(
    django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks,
) -> None:
    # Given the row-level-security allow-list
    # When the table is classified
    assert is_exempt_from_tenant_policy("audit_datasubjectrequest")

    # Then, unlike the audit tables, its rows can be erased — which is the point, as
    # it holds a CPF and the subject may later ask for it to be removed
    with django_capture_on_commit_callbacks(execute=True):
        Client().post(DSR_URL, VALID_REQUEST)
    assert DataSubjectRequest.objects.count() == 1
    DataSubjectRequest.objects.all().delete()
    assert DataSubjectRequest.objects.count() == 0


def test_notification_status_is_displayed_read_only_in_the_admin() -> None:
    # Given the operator-facing request admin
    model_admin = DataSubjectRequestAdmin(DataSubjectRequest, admin.site)
    notification_fields = {
        "encarregado_notified_at",
        "notification_last_error",
    }

    # Then delivery state is visible but neither field can be edited
    assert notification_fields <= set(model_admin.list_display)
    assert notification_fields <= set(model_admin.readonly_fields)


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
                # ALL_OBJECTS_OK: the scan must reach every row this tenant holds,
                # and RLS still bounds it to the context entered above.
                _values_of(Event.all_objects.values_list("metadata", flat=True)),
            )
    return values


def _document_shaped(values: list[str]) -> list[str]:
    return [v for v in values if CPF_SHAPE.search(v) or CNPJ_SHAPE.search(v)]


# Every form a document is written in anywhere near this product. The two BARE CNPJs
# are the load-bearing entries: `normalize_document` strips the mask, so those are the
# only forms a column — and therefore a leak out of one — can hold. The masked forms
# come back out of `apps.fiscal.formatting`, so a value copied off a rendered page is
# in scope too.
STORED_DOCUMENTS = (
    "12ABC34501DE35",
    "12345678000195",
    "52998224725",
)
MASKED_DOCUMENTS = (
    "12.ABC.345/01DE-35",
    "12.345.678/0001-95",
    "529.982.247-25",
)

# What the audit tables genuinely do carry. A guard that fires on any of these would
# be reverted the first time it went red on a release, which is a slower way of having
# no guard at all — so the widening above is asserted not to have reached them.
ORDINARY_METADATA = (
    "ClientCompany",
    "owner",
    "invite_issued",
    "dsr_submitted",
    "EXPORT",
    "csv",
    "12",
    "0198f3c1-6a2b-7c4d-8e9f-0a1b2c3d4e5f",
    "0198F3C1-6A2B-7C4D-8E9F-0A1B2C3D4E5F",
    "0198f3c16a2b7c4d8e9f0a1b2c3d4e5f",
    "0198F3C16A2B7C4D8E9F0A1B2C3D4E5F",
    "2026-07-28T14:30:00+00:00",
    "2026-07-28 14:30:00.123456+00:00",
    "2026-07-28",
    "1785678901.123456",
    "maria@exemplo.example",
)


@pytest.mark.parametrize("document", STORED_DOCUMENTS)
def test_the_patterns_match_a_document_in_the_form_it_is_stored_in(
    document: str,
) -> None:
    """The half that was blind. Storage is normalized; the mask never reaches a column.

    A CNPJ pattern that required `.`, `/` and `-` matched only the display form, so
    the scan below ran over real rows unable to recognise the one shape a leak would
    take. It passed for exactly that reason, which is the failure mode this project
    keeps finding: a test that asserts nothing while looking like it asserts a lot.
    """
    # Given a document number in the form `normalize_document` leaves behind
    # When the patterns are applied
    # Then it is recognised
    assert _document_shaped([document])


@pytest.mark.parametrize("document", MASKED_DOCUMENTS)
def test_the_patterns_match_a_document_in_the_form_it_is_displayed_in(
    document: str,
) -> None:
    # Given a document number as `apps.fiscal.formatting` renders it
    # When the patterns are applied
    # Then it is recognised, so a value copied off a page is in scope too
    assert _document_shaped([document])


@pytest.mark.parametrize("value", ORDINARY_METADATA)
def test_the_patterns_ignore_the_values_the_audit_tables_actually_carry(
    value: str,
) -> None:
    """The other direction: widening the CNPJ pattern must not have cost precision.

    Both patterns still demand a full-length run bounded by `\\b` and ending in two
    digits. No UUID in any casing, no timestamp, no action slug and no identifier this
    product writes can satisfy that — and a guard that fired on one of them would be
    switched off rather than fixed.
    """
    # Given a value the audit trail legitimately holds
    # When the patterns are applied
    # Then it is not mistaken for a document number
    assert not _document_shaped([value])


def test_no_audit_metadata_value_is_document_shaped(
    tenant: Tenant,
    django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks,
) -> None:
    # Given audit rows written through the product's real paths
    with django_capture_on_commit_callbacks(execute=True):
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


def test_the_scan_catches_a_document_written_the_way_a_leak_would_write_it(
    tenant: Tenant,
) -> None:
    """The mutation, kept as a test: a real leak, through the real write path.

    `metadata={"cnpj": company.cnpj}` is the mistake that will actually be made one
    day, and the value it carries is the normalized one, because that is what the
    column holds. Against the CNPJ pattern as originally written this scan ran over
    that row and found nothing — the guard above passed while blind to the only shape
    a leak takes. Asserting the scan is *capable* of failing is the half that was
    missing; without it, `assert not offenders` is satisfied by a regex that matches
    nothing at all.
    """
    # Given an audit event carrying a document number in the form a column holds it
    with tenant_context(tenant.id):
        record_event(
            action=AuditAction.EXPORT,
            obj=ObjectRef(type="ClientCompany", id=str(tenant.pk)),
            metadata={"cnpj": "12ABC34501DE35"},
        )

    # When the guard's own scan is applied, unchanged
    offenders = _document_shaped(_tenant_metadata_values())

    # Then it finds it
    assert offenders == ["12ABC34501DE35"]
