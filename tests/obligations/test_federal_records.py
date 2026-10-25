"""AXRM-014: federal consultation receipts, payments and fiscal-situation reports.

Acceptance for plan todo 14: uniqueness, append-only, the production-only gate on
official observations (INV-R4), "nada localizado" as a receipt and never a payment
observation, Decimal amounts from JSON numbers, `source_subjects` inclusion and the
render policy for consultation receipts.
"""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.cycles.completion_sources import source_subjects
from apps.integrations.call_models import ProviderCall
from apps.integrations.models import (
    ClientProviderLink,
    ProviderConnection,
    ProviderEnvironment,
)
from apps.obligations.coverage import coverage_for
from apps.obligations.federal import (
    ConsultationCall,
    FederalRecordConflictError,
    amount,
    payment_from_provider,
    record_payment_consultation,
)
from apps.obligations.generator import generate_das_calendar
from apps.obligations.models import (
    Document,
    FederalConsultation,
    FederalConsultationOutcome,
    FederalEnvironment,
    FederalPayment,
    FiscalSituationReport,
    Obligation,
    SourceObservation,
)
from apps.obligations.presentation import (
    FEDERAL_STATES,
    federal_consultation_state,
)
from tests.cycles.cycle_fixtures import PERIOD
from tests.cycles.observation_fixtures import payment_claim
from tests.portal.request_fixtures import PortalSetup, portal_setup

pytestmark = pytest.mark.django_db

NUMERO = "07202609000123456"
NEXT_MONTH = date(2026, 10, 1)


@pytest.fixture
def federal_case(settings: SettingsWrapper, tmp_path: Path) -> PortalSetup:
    """A MEI client with its DAS calendar and an open cycle no request binds to DAS."""
    settings.MEDIA_ROOT = tmp_path
    setup = portal_setup("axrm014")
    with tenant_context(setup.graph.tenant.pk):
        generate_das_calendar(setup.graph.client, starting_from=PERIOD)
    return setup


def _das(client: ClientCompany, competence: date = PERIOD) -> Obligation:
    return Obligation.objects.get(
        client=client, competence_month=competence, obligation_type_id="DAS"
    )


def _provider_item(
    total: object = 76.6, *, numero: str = NUMERO, periodo: str = "202609"
) -> dict[str, object]:
    """One PAGTOWEB entry exactly as `json.loads` hands it over (JSON numbers)."""
    parsed: dict[str, object] = json.loads(
        json.dumps(
            {
                "tipo": "DAS",
                "numeroDocumento": numero,
                "periodoApuracao": periodo,
                "dataArrecadacao": "2026-10-15",
                "dataVencimento": "2026-10-20",
                "receita": {"codigo": "1007", "descricao": "DAS MEI"},
                "valorTotal": total,
                "valorPrincipal": total,
                "valorMulta": 0,
                "valorJuros": 0,
            }
        )
    )
    return parsed


def _call(environment: str = "producao", ref: str = "call-1") -> ConsultationCall:
    return ConsultationCall(
        range_start=PERIOD,
        range_end=date(2026, 10, 31),
        consulted_at=timezone.now().replace(microsecond=0),
        environment=environment,
        provider_ref=ref,
        scope_digest=sha256(b"synthetic federal range").hexdigest(),
    )


def _persisted_provider_call(
    client: ClientCompany,
    environment: str = "producao",
    ref: str = "call-1",
    *,
    state: str = "sucesso",
    finalized: bool = True,
) -> ProviderCall:
    """Seed the actual scoped protocol row, not a caller's assertion of provenance."""
    provider, _ = ProviderConnection.objects.get_or_create(
        tenant_id=client.tenant_id,
        provider="serpro",
        environment=environment,
        defaults={"status": "ativa"},
    )
    link, _ = ClientProviderLink.objects.get_or_create(
        tenant_id=client.tenant_id,
        client=client,
        connection=provider,
        defaults={"provider_company_ref": str(client.pk), "status": "ativa"},
    )
    row, _ = ProviderCall.objects.get_or_create(
        tenant_id=client.tenant_id,
        client=client,
        request_key=sha256(ref.encode()).hexdigest(),
        defaults={
            "link": link,
            "operation": "SP-PAG",
            "operation_revision": "v1",
            "operation_class": "read",
            "provider": "serpro",
            "environment": environment,
            "request_digest": sha256(b"synthetic federal range").hexdigest(),
            "business_effect_key": sha256(ref.encode()).hexdigest(),
            "state": state,
            "finalized_at": timezone.now() if finalized else None,
            "billing_class": "consulta",
            "billed": "nao",
        },
    )
    return row


def _finalized_call(
    client: ClientCompany, environment: str = "producao", ref: str = "call-1"
) -> ConsultationCall:
    row = _persisted_provider_call(client, environment, ref)
    assert row.finalized_at is not None
    return replace(_call(environment, str(row.pk)), consulted_at=row.finalized_at)


def _payment_observations(subject: Obligation) -> list[SourceObservation]:
    return list(SourceObservation.objects.filter(subject_ref=subject, kind="payment"))


def _coverage_state(setup: PortalSetup) -> str:
    return coverage_for(
        setup.graph.actor,
        setup.graph.client,
        PERIOD,
        engagement_id=setup.cycle.engagement_id,
    )[0].state


def _counts() -> tuple[int, int, int]:
    return (
        FederalConsultation.objects.count(),
        FederalPayment.objects.count(),
        SourceObservation.objects.filter(kind="payment").count(),
    )


def test_production_das_writes_the_payment_and_one_verified_official_observation(
    federal_case: PortalSetup, settings: SettingsWrapper
) -> None:
    # Given a production call with the owner's real-data approval and one DAS
    settings.PROVIDER_REAL_DATA_APPROVED = True
    with tenant_context(federal_case.graph.tenant.pk):
        subject = _das(federal_case.graph.client)
        assert _coverage_state(federal_case) == "unknown"
        call = _finalized_call(federal_case.graph.client)
        record = payment_from_provider(
            _provider_item(), matched_obligation_id=subject.pk
        )

        # When the consultation is recorded
        consultation = record_payment_consultation(
            federal_case.graph.client, call, [record]
        )

        # Then the receipt, the payment and one official verified fact exist
        assert consultation.outcome == FederalConsultationOutcome.ENCONTRADO
        assert consultation.environment == FederalEnvironment.PRODUCAO
        payment = FederalPayment.objects.get()
        assert payment.consultation_id == consultation.pk
        assert payment.matched_obligation_id == subject.pk
        assert payment.valor_total == Decimal("76.60")
        assert payment.periodo_apuracao == PERIOD
        facts = _payment_observations(subject)
        assert len(facts) == 1
        fact = facts[0]
        assert fact.source_kind == "official_api"
        assert fact.verification_status == "verified"
        assert fact.source_ref == NUMERO
        assert fact.observed_at == call.consulted_at
        assert fact.period == subject.competence_month
        assert fact.value_json["consultation_id"] == str(consultation.pk)
        assert _coverage_state(federal_case) == "verified"


@pytest.mark.parametrize(
    ("environment", "approved"),
    [
        ("fake", True),
        ("trial", True),
        ("homologacao", True),
        ("producao", False),
    ],
)
def test_non_production_or_unapproved_results_are_recorded_but_never_verify(
    federal_case: PortalSetup,
    settings: SettingsWrapper,
    environment: str,
    approved: bool,
) -> None:
    # Given the same DAS result from an environment that may not verify anything
    settings.PROVIDER_REAL_DATA_APPROVED = approved
    with tenant_context(federal_case.graph.tenant.pk):
        subject = _das(federal_case.graph.client)
        before = _coverage_state(federal_case)
        record = payment_from_provider(
            _provider_item(), matched_obligation_id=subject.pk
        )

        # When it is recorded
        consultation = record_payment_consultation(
            federal_case.graph.client,
            _finalized_call(federal_case.graph.client, environment),
            [record],
        )

        # Then the records exist, no observation was written, coverage is unchanged
        assert consultation.outcome == FederalConsultationOutcome.ENCONTRADO
        assert FederalPayment.objects.get().matched_obligation_id == subject.pk
        assert _payment_observations(subject) == []
        assert _coverage_state(federal_case) == before == "unknown"


def test_an_empty_result_is_a_nada_localizado_receipt_and_no_observation(
    federal_case: PortalSetup, settings: SettingsWrapper
) -> None:
    # Given a production call that found nothing
    settings.PROVIDER_REAL_DATA_APPROVED = True
    with tenant_context(federal_case.graph.tenant.pk):
        subject = _das(federal_case.graph.client)

        # When it is recorded
        consultation = record_payment_consultation(
            federal_case.graph.client, _finalized_call(federal_case.graph.client), []
        )

        # Then it is a receipt only: nothing was found, nothing is claimed
        assert consultation.outcome == FederalConsultationOutcome.NADA_LOCALIZADO
        assert FederalPayment.objects.count() == 0
        assert _payment_observations(subject) == []
        assert _coverage_state(federal_case) == "unknown"


def test_a_failed_call_is_a_receipt_and_cannot_carry_payments(
    federal_case: PortalSetup,
) -> None:
    with tenant_context(federal_case.graph.tenant.pk):
        record = payment_from_provider(_provider_item())
        # A failure outcome with payments, or an unknown failure word, writes nothing.
        for failure, payments in (("falha", [record]), ("encontrado", [])):
            with pytest.raises(ValidationError):
                record_payment_consultation(
                    federal_case.graph.client,
                    _call(ref=""),
                    payments,
                    failure=failure,
                )
        assert _counts() == (0, 0, 0)

        consultation = record_payment_consultation(
            federal_case.graph.client, _call(ref=""), failure="sem_procuracao"
        )
        assert consultation.outcome == FederalConsultationOutcome.SEM_PROCURACAO
        assert FederalPayment.objects.count() == 0


def test_a_replayed_call_returns_its_receipt_without_new_rows(
    federal_case: PortalSetup, settings: SettingsWrapper
) -> None:
    settings.PROVIDER_REAL_DATA_APPROVED = True
    with tenant_context(federal_case.graph.tenant.pk):
        subject = _das(federal_case.graph.client)
        record = payment_from_provider(
            _provider_item(), matched_obligation_id=subject.pk
        )
        first = record_payment_consultation(
            federal_case.graph.client,
            _finalized_call(federal_case.graph.client),
            [record],
        )

        again = record_payment_consultation(
            federal_case.graph.client,
            _finalized_call(federal_case.graph.client),
            [record],
        )

        assert again.pk == first.pk
        assert _counts() == (1, 1, 1)


def test_a_later_consultation_that_disagrees_with_a_known_document_writes_nothing(
    federal_case: PortalSetup, settings: SettingsWrapper
) -> None:
    settings.PROVIDER_REAL_DATA_APPROVED = True
    with tenant_context(federal_case.graph.tenant.pk):
        subject = _das(federal_case.graph.client)
        record = payment_from_provider(
            _provider_item(), matched_obligation_id=subject.pk
        )
        record_payment_consultation(
            federal_case.graph.client,
            _finalized_call(federal_case.graph.client),
            [record],
        )
        changed = replace(record, valor_total=Decimal("80.00"))

        with pytest.raises(FederalRecordConflictError):
            record_payment_consultation(
                federal_case.graph.client,
                _finalized_call(federal_case.graph.client, ref="call-2"),
                [changed],
            )

        assert _counts() == (1, 1, 1)
        assert FederalPayment.objects.get().valor_total == Decimal("76.60")


def test_a_match_must_answer_the_obligation_competence(
    federal_case: PortalSetup,
) -> None:
    with tenant_context(federal_case.graph.tenant.pk):
        subject = _das(federal_case.graph.client)
        record = payment_from_provider(
            _provider_item(periodo="202610"), matched_obligation_id=subject.pk
        )

        with pytest.raises(ValidationError):
            record_payment_consultation(
                federal_case.graph.client,
                _finalized_call(federal_case.graph.client),
                [record],
            )

        assert _counts() == (0, 0, 0)


def test_document_numbers_are_unique_per_client_not_per_firm(
    federal_case: PortalSetup,
) -> None:
    graph = federal_case.graph
    with tenant_context(graph.tenant.pk):
        record = payment_from_provider(_provider_item())
        record_payment_consultation(
            graph.client, _finalized_call(graph.client), [record]
        )
        consultation = FederalConsultation.objects.get()
        fields = {
            "tenant_id": graph.tenant.pk,
            "numero_documento": NUMERO,
            "tipo": "DAS",
            "periodo_apuracao": PERIOD,
            "data_arrecadacao": date(2026, 10, 15),
            "receita_codigo": "1007",
            "valor_total": Decimal("1.00"),
        }

        with pytest.raises(IntegrityError) as refused, transaction.atomic():
            FederalPayment.objects.create(
                client=graph.client, consultation=consultation, **fields
            )
        assert "federalpayment_document_uniq" in str(refused.value)

        sibling = federal_case.other_client
        sibling_receipt = record_payment_consultation(
            sibling, _finalized_call(sibling, ref="call-s")
        )
        FederalPayment.objects.create(
            client=sibling, consultation=sibling_receipt, **fields
        )
        assert FederalPayment.objects.filter(numero_documento=NUMERO).count() == 2


def test_federal_payments_are_append_only_for_every_role_including_the_owner(
    federal_case: PortalSetup,
) -> None:
    # Given one seeded payment row
    with tenant_context(federal_case.graph.tenant.pk):
        record_payment_consultation(
            federal_case.graph.client,
            _finalized_call(federal_case.graph.client),
            [payment_from_provider(_provider_item())],
        )
        payment = FederalPayment.objects.get()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_trigger WHERE tgname = %s AND NOT tgisinternal",
            ["obligations_federalpayment_append_only"],
        )
        assert cursor.fetchone() == (1,)
        cursor.execute(
            "SELECT p, has_table_privilege('app_runtime', "
            "'obligations_federalpayment', p) "
            "FROM unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE']) p"
        )
        assert dict(cursor.fetchall()) == {
            "SELECT": True,
            "INSERT": True,
            "UPDATE": False,
            "DELETE": False,
            "TRUNCATE": False,
        }

    # When the table OWNER (app_test, BYPASSRLS like app_migrator) rewrites or
    # deletes it, grants cannot mask a missing trigger
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
    try:
        for statement in (
            "UPDATE obligations_federalpayment SET valor_total = 0 WHERE id = %s",
            "DELETE FROM obligations_federalpayment WHERE id = %s",
        ):
            with (
                pytest.raises(DatabaseError, match="append-only"),
                transaction.atomic(),
                connection.cursor() as cursor,
            ):
                cursor.execute(statement, [payment.pk])
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE app_runtime")

    # Then the row is unchanged
    with tenant_context(federal_case.graph.tenant.pk):
        assert FederalPayment.objects.get().valor_total == Decimal("76.60")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (76.6, Decimal("76.60")),
        (0.1, Decimal("0.10")),
        (1234.5, Decimal("1234.50")),
        (100, Decimal("100.00")),
        ("76.60", Decimal("76.60")),
    ],
)
def test_json_numbers_become_exact_cents_through_str(
    raw: object, expected: Decimal
) -> None:
    converted = amount(json.loads(json.dumps(raw)))

    assert converted == expected
    assert str(converted) == str(expected)


@pytest.mark.parametrize(
    "raw", [76.555, -1, True, None, "abc", float("nan"), "1e400", "1e-400"]
)
def test_malformed_or_sub_cent_amounts_are_refused_never_rounded(raw: object) -> None:
    with pytest.raises(ValidationError):
        amount(raw)


def test_a_provider_entry_normalizes_every_field() -> None:
    record = payment_from_provider(_provider_item())

    assert record.numero_documento == NUMERO
    assert record.tipo == "DAS"
    assert record.periodo_apuracao == PERIOD
    assert record.data_arrecadacao == date(2026, 10, 15)
    assert record.data_vencimento == date(2026, 10, 20)
    assert record.receita_codigo == "1007"
    assert record.receita_descricao == "DAS MEI"
    assert (record.valor_principal, record.valor_multa, record.valor_juros) == (
        Decimal("76.60"),
        Decimal("0.00"),
        Decimal("0.00"),
    )
    with pytest.raises(ValidationError):
        payment_from_provider({**_provider_item(), "tipo": "GPS"})


def test_source_subjects_include_obligations_with_federal_observations(
    federal_case: PortalSetup, settings: SettingsWrapper
) -> None:
    settings.PROVIDER_REAL_DATA_APPROVED = True
    graph = federal_case.graph
    with tenant_context(graph.tenant.pk):
        subject = _das(graph.client)
        october = _das(graph.client, NEXT_MONTH)
        assert subject.pk not in source_subjects(federal_case.cycle)

        # Controls the filters must exclude: another client's federal fact in the
        # same period, this client's federal fact for another period, and official
        # observations no federal payment produced -- one citing a real federal
        # document number of ANOTHER obligation, one on an obligation a (trial)
        # payment matched but citing an unrelated reference.
        sibling = federal_case.other_client
        sibling_das = Obligation.objects.create(
            tenant=graph.tenant,
            client=sibling,
            obligation_type_id="DAS",
            competence_month=PERIOD,
            nominal_due_date=date(2026, 10, 20),
            resolved_due_date=date(2026, 10, 20),
        )
        record_payment_consultation(
            sibling,
            _finalized_call(sibling, ref="call-s"),
            [
                payment_from_provider(
                    _provider_item(), matched_obligation_id=sibling_das.pk
                )
            ],
        )
        record_payment_consultation(
            graph.client,
            _finalized_call(graph.client, ref="call-oct"),
            [
                payment_from_provider(
                    _provider_item(numero="07202610000999", periodo="202610"),
                    matched_obligation_id=october.pk,
                )
            ],
        )
        dasn = Obligation.objects.create(
            tenant=graph.tenant,
            client=graph.client,
            obligation_type_id="DASN",
            competence_month=PERIOD,
            nominal_due_date=date(2027, 5, 31),
            resolved_due_date=date(2027, 5, 31),
        )
        payment_claim(dasn, source="official_api", verification="verified")
        SourceObservation.objects.create(
            tenant_id=graph.tenant.pk,
            client_id=graph.client.pk,
            subject_ref=dasn,
            kind="payment",
            period=PERIOD,
            source_kind="official_api",
            verification_status="verified",
            observed_at=timezone.now(),
            source_ref=NUMERO,
        )
        record_payment_consultation(
            graph.client,
            _finalized_call(graph.client, "trial", ref="call-trial"),
            [
                payment_from_provider(
                    _provider_item(numero="07202609000777"),
                    matched_obligation_id=dasn.pk,
                )
            ],
        )
        assert source_subjects(federal_case.cycle).isdisjoint(
            {subject.pk, sibling_das.pk, october.pk, dasn.pk}
        )

        # When this client's own September consultation verifies its DAS
        record_payment_consultation(
            graph.client,
            _finalized_call(graph.client),
            [payment_from_provider(_provider_item(), matched_obligation_id=subject.pk)],
        )

        # Then exactly that obligation joins the cycle's source subjects
        subjects = source_subjects(federal_case.cycle)
        assert subject.pk in subjects
        assert subjects.isdisjoint({sibling_das.pk, october.pk, dasn.pk})


@pytest.mark.parametrize("outcome", FederalConsultationOutcome.values)
@pytest.mark.parametrize("environment", FederalEnvironment.values)
@pytest.mark.parametrize("approved", [True, False])
def test_a_receipt_renders_verified_only_when_production_found_payments(
    outcome: str, environment: str, approved: bool
) -> None:
    state = federal_consultation_state(
        outcome, environment, real_data_approved=approved
    )

    assert state in FEDERAL_STATES
    expected_verified = (outcome, environment, approved) == (
        "encontrado",
        "producao",
        True,
    )
    assert (state == "verified") is expected_verified
    if outcome == "nada_localizado":
        assert state == "unknown"


def test_the_receipt_render_vocabulary_is_explicit() -> None:
    def state(outcome: str) -> str:
        return federal_consultation_state(outcome, "producao", real_data_approved=True)

    assert [state(value) for value in FederalConsultationOutcome.values] == [
        "verified",
        "unknown",
        "unauthorized",
        "refresh_failed",
        "refresh_failed",
        "uncertain",
    ]
    assert state("an-unknown-outcome") == "unknown"


def test_the_environment_vocabulary_mirrors_the_provider_environments() -> None:
    assert FederalEnvironment.values == ProviderEnvironment.values


def _report_document(setup: PortalSetup, *, visible: bool) -> Document:
    return Document.objects.create(
        tenant=setup.graph.tenant,
        client=setup.graph.client,
        sha256=("b" if visible else "c") * 64,
        original_filename="sitfis.pdf",
        content_type="application/pdf",
        byte_size=10,
        uploaded_by=setup.graph.actor,
        client_upload=False,
        portal_visible=visible,
        acquisition_origin="upload_equipe",
    )


def test_a_situation_report_caps_attempts_and_keeps_its_document_firm_only(
    federal_case: PortalSetup,
) -> None:
    graph = federal_case.graph
    with tenant_context(graph.tenant.pk):
        receipt = FederalConsultation.objects.create(
            tenant=graph.tenant,
            client=graph.client,
            kind="situacao_fiscal",
            scope_digest="a" * 64,
            consulted_at=datetime(2026, 10, 1, 9, tzinfo=UTC),
            outcome="encontrado",
            environment="fake",
        )
        base = {
            "tenant": graph.tenant,
            "client": graph.client,
            "consultation": receipt,
            "requested_at": receipt.consulted_at,
        }
        with pytest.raises(IntegrityError) as capped, transaction.atomic():
            FiscalSituationReport.objects.create(**base, emission_attempts=4)
        assert "fiscalsituationreport_attempt_cap" in str(capped.value)

        visible = _report_document(federal_case, visible=True)
        with pytest.raises(DatabaseError, match="firm-only"), transaction.atomic():
            FiscalSituationReport.objects.create(**base, document=visible)

        report = FiscalSituationReport.objects.create(
            **base,
            emission_attempts=3,
            document=_report_document(federal_case, visible=False),
            state="emitido",
            emitted_at=receipt.consulted_at + timedelta(minutes=1),
        )
        with pytest.raises(DatabaseError, match="firm-only"), transaction.atomic():
            FiscalSituationReport.objects.filter(pk=report.pk).update(document=visible)
        assert FiscalSituationReport.objects.get().state == "emitido"


def test_a_consultation_range_follows_its_kind(federal_case: PortalSetup) -> None:
    graph = federal_case.graph
    with tenant_context(graph.tenant.pk):
        common = {
            "tenant": graph.tenant,
            "client": graph.client,
            "consulted_at": datetime(2026, 10, 1, 9, tzinfo=UTC),
            "outcome": "nada_localizado",
            "environment": "fake",
            "scope_digest": "a" * 64,
        }
        for kind, start, end in (
            ("pagamentos", None, None),
            ("pagamentos", NEXT_MONTH, PERIOD),
            ("situacao_fiscal", PERIOD, NEXT_MONTH),
        ):
            with pytest.raises(IntegrityError) as refused, transaction.atomic():
                FederalConsultation.objects.create(
                    **common, kind=kind, range_start=start, range_end=end
                )
            assert "federalconsultation_range_by_kind" in str(refused.value)
