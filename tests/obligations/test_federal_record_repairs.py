"""AXRM-014 repair: persisted provenance, durable scope and private report originals."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.obligations.federal import payment_from_provider, record_payment_consultation
from apps.obligations.models import (
    Document,
    FederalConsultation,
    FiscalSituationReport,
    SourceObservation,
)
from tests.cycles.cycle_fixtures import cycle_setup
from tests.obligations.test_federal_records import (
    _call,
    _counts,
    _coverage_state,
    _das,
    _finalized_call,
    _persisted_provider_call,
    _provider_item,
    _report_document,
    federal_case,
)
from tests.portal.request_fixtures import PortalSetup

__all__ = ["federal_case"]
pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("environment", ["fake", "trial", "homologacao"])
def test_forged_production_metadata_is_refused_without_writes(
    federal_case: PortalSetup, settings: SettingsWrapper, environment: str
) -> None:
    # Given an actual finalized non-production call and forged caller metadata.
    settings.PROVIDER_REAL_DATA_APPROVED = True
    graph = federal_case.graph
    with tenant_context(graph.tenant.pk):
        actual = _persisted_provider_call(graph.client, environment)
        record = payment_from_provider(
            _provider_item(), matched_obligation_id=_das(graph.client).pk
        )
        before = _counts()

        # When the caller falsely declares that call production.
        with pytest.raises(ValidationError):
            record_payment_consultation(
                graph.client, _call("producao", ref=str(actual.pk)), [record]
            )

        # Then neither receipts nor official facts or coverage have changed.
        assert _counts() == before
        assert _coverage_state(federal_case) == "unknown"


@pytest.mark.parametrize(
    ("state", "finalized"),
    [
        ("reservada", False),
        ("sucesso", False),
        ("falhou", True),
        ("incerta", True),
        ("reconciliada", True),
    ],
)
def test_only_successfully_finalized_calls_can_supply_payment_results(
    federal_case: PortalSetup,
    settings: SettingsWrapper,
    state: str,
    finalized: bool,
) -> None:
    # Given a production row that does not establish successful finalization.
    settings.PROVIDER_REAL_DATA_APPROVED = True
    graph = federal_case.graph
    with tenant_context(graph.tenant.pk):
        actual = _persisted_provider_call(
            graph.client, state=state, finalized=finalized
        )
        record = payment_from_provider(
            _provider_item(), matched_obligation_id=_das(graph.client).pk
        )

        # When a successful payment result is asserted for it.
        with pytest.raises(ValidationError):
            record_payment_consultation(
                graph.client, _call(ref=str(actual.pk)), [record]
            )

        # Then the unsuccessful call has produced no facts or durable result.
        assert _counts() == (0, 0, 0)
        assert _coverage_state(federal_case) == "unknown"


@pytest.mark.parametrize(
    "reference", ["", "not-a-call", "00000000-0000-0000-0000-000000000014"]
)
def test_success_requires_a_resolvable_call_reference(
    federal_case: PortalSetup, reference: str
) -> None:
    # Given no persisted provider call matching the supplied reference.
    with tenant_context(federal_case.graph.tenant.pk):
        # When even an empty successful result is asserted.
        with pytest.raises(ValidationError):
            record_payment_consultation(federal_case.graph.client, _call(ref=reference))
        # Then it has not become a successful production receipt.
        assert _counts() == (0, 0, 0)


def test_a_sibling_clients_call_cannot_supply_this_clients_result(
    federal_case: PortalSetup,
) -> None:
    # Given a genuine call belonging to a different client in the same firm.
    graph = federal_case.graph
    with tenant_context(graph.tenant.pk):
        other = _persisted_provider_call(federal_case.other_client)
        # When its id is presented as this client's call.
        with pytest.raises(ValidationError):
            record_payment_consultation(graph.client, _call(ref=str(other.pk)))
        # Then the client predicate has refused it without writing a receipt.
        assert _counts() == (0, 0, 0)


@pytest.mark.parametrize("role", ["app_runtime", "app_test"])
def test_a_foreign_tenants_call_is_refused_even_under_the_table_owner(
    federal_case: PortalSetup, role: str
) -> None:
    # Given an actual call in a different firm, not merely an unknown UUID.
    other = cycle_setup("axrm014-foreign-call")
    with tenant_context(other.tenant.pk):
        foreign = _persisted_provider_call(other.client)
    with tenant_context(federal_case.graph.tenant.pk):
        with connection.cursor() as cursor:
            cursor.execute(
                "RESET ROLE" if role == "app_test" else "SET ROLE app_runtime"
            )
        try:
            # When its id is presented in this firm's write contract.
            with pytest.raises(ValidationError):
                record_payment_consultation(
                    federal_case.graph.client, _call(ref=str(foreign.pk))
                )
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SET ROLE app_runtime")
        # Then application scope also holds when the effective role bypasses RLS.
        assert _counts() == (0, 0, 0)


@pytest.mark.parametrize("role", ["app_runtime", "app_test"])
def test_an_attached_report_document_cannot_later_become_portal_visible(
    federal_case: PortalSetup, role: str
) -> None:
    # Given a report linked to a private original, including under the table owner.
    graph = federal_case.graph
    with tenant_context(graph.tenant.pk):
        receipt = record_payment_consultation(
            graph.client, _finalized_call(graph.client, "fake", ref="private-report")
        )
        document = _report_document(federal_case, visible=False)
        report = FiscalSituationReport.objects.create(
            tenant=graph.tenant,
            client=graph.client,
            consultation=receipt,
            requested_at=receipt.consulted_at,
            document=document,
            state="emitido",
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "RESET ROLE" if role == "app_test" else "SET ROLE app_runtime"
            )
            cursor.execute("SELECT current_user")
            assert cursor.fetchone() == (role,)
        try:
            # When the document itself is changed after attachment.
            with pytest.raises(DatabaseError), transaction.atomic():
                Document.objects.filter(pk=document.pk).update(portal_visible=True)
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SET ROLE app_runtime")
        # Then the report's actual stored document remains private.
        document.refresh_from_db()
        report.refresh_from_db()
        assert not document.portal_visible
        assert report.document_id == document.pk


def test_consultation_scope_digest_survives_a_database_reload(
    federal_case: PortalSetup,
) -> None:
    # Given the durable field and the canonical digest of an actual request scope.
    assert "scope_digest" in {
        field.name for field in FederalConsultation._meta.get_fields()
    }
    graph = federal_case.graph
    with tenant_context(graph.tenant.pk):
        actual = _persisted_provider_call(graph.client, "trial")
        call = replace(
            _call("trial", ref=str(actual.pk)), scope_digest=actual.request_digest
        )
        # When the writer commits a receipt and it is fetched afresh.
        receipt = record_payment_consultation(graph.client, call)
        stored = FederalConsultation.objects.get(pk=receipt.pk)
        # Then the query scope is durable independently of dates and the opaque id.
        assert stored.scope_digest == actual.request_digest
        assert not SourceObservation.objects.filter(kind="payment").exists()


@pytest.mark.parametrize("field", ["environment", "scope_digest"])
def test_inconsistent_metadata_is_refused_even_on_an_existing_receipt(
    federal_case: PortalSetup, field: str
) -> None:
    # Given a recorded real call and a replay whose metadata disagrees with it.
    graph = federal_case.graph
    with tenant_context(graph.tenant.pk):
        call = _finalized_call(graph.client, "trial")
        first = record_payment_consultation(graph.client, call)
        forged = (
            replace(call, environment="producao")
            if field == "environment"
            else replace(call, scope_digest="b" * 64)
        )
        # When the forged replay reaches the writer.
        with pytest.raises(ValidationError):
            record_payment_consultation(graph.client, forged)
        # Then the original durable receipt is not replaced or misrepresented.
        assert _counts() == (1, 0, 0)
        assert FederalConsultation.objects.get().pk == first.pk


@pytest.mark.parametrize(
    "digest", ["", "a" * 63, "A" * 64, "g" * 64, "a" * 65, "a" * 63 + "\n"]
)
def test_invalid_scope_digests_are_refused_before_writes(
    federal_case: PortalSetup, digest: str
) -> None:
    # Given a well-formed refused-before-dispatch query except for its scope digest.
    with tenant_context(federal_case.graph.tenant.pk):
        call = replace(_call("fake", ref=""), scope_digest=digest)
        # When the receipt is recorded.
        with pytest.raises(ValidationError):
            record_payment_consultation(
                federal_case.graph.client, call, failure="cota_esgotada"
            )
        # Then malformed scopes are not durably accepted.
        assert _counts() == (0, 0, 0)


@pytest.mark.parametrize(
    ("field", "value"),
    [("operation", "SP-SIT-P"), ("provider", "focus"), ("operation_class", "issuance")],
)
def test_a_different_provider_operation_cannot_supply_a_payment_result(
    federal_case: PortalSetup, field: str, value: str
) -> None:
    # Given a genuine scoped finalized call for something other than payment lookup.
    graph = federal_case.graph
    with tenant_context(graph.tenant.pk):
        actual = _persisted_provider_call(graph.client)
        setattr(actual, field, value)
        actual.save(update_fields=[field])
        # When its id is submitted to the payment writer.
        with pytest.raises(ValidationError):
            record_payment_consultation(graph.client, _call(ref=str(actual.pk)))
        # Then the unrelated call cannot be turned into federal payment evidence.
        assert _counts() == (0, 0, 0)


def test_the_receipt_clock_comes_from_persisted_finalization(
    federal_case: PortalSetup,
) -> None:
    # Given a genuine finalized call and an arbitrary caller-supplied clock.
    graph = federal_case.graph
    with tenant_context(graph.tenant.pk):
        actual = _persisted_provider_call(graph.client, "trial")
        call = replace(
            _call("trial", ref=str(actual.pk)),
            consulted_at=datetime(2000, 1, 1, tzinfo=UTC),
        )
        # When the receipt is recorded.
        receipt = record_payment_consultation(graph.client, call)
        # Then its durable observation time is that of the actual finalization.
        receipt.refresh_from_db()
        assert receipt.consulted_at == actual.finalized_at


def test_unattached_documents_can_still_change_visibility(
    federal_case: PortalSetup,
) -> None:
    # Given a private staff document that is not a report original.
    with tenant_context(federal_case.graph.tenant.pk):
        document = _report_document(federal_case, visible=False)
        # When an authorized firm writer changes its visibility.
        Document.objects.filter(pk=document.pk).update(portal_visible=True)
        # Then the report-specific guard has not forbidden unrelated publications.
        document.refresh_from_db()
        assert document.portal_visible
