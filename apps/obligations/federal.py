"""Write federal consultation receipts and, from production only, official facts.

AXRM-014 (plan todo 14; IS-2, INV-R4). The provider slices (todos 15-18) call these
after a SERPRO call finalizes; nothing here performs I/O.

* `payment_from_provider` turns one PAGTOWEB `DocumentoArrecadacaoIC` entry into a
  typed record. Amounts arriving as JSON numbers are converted through `str`, never
  through `float` arithmetic, and a value that does not fit two decimal places is
  refused rather than rounded.
* `record_payment_consultation` writes the receipt and its payments in one
  transaction. Its outcome is derived, not trusted: payments make `encontrado`, none
  make `nada_localizado` -- a receipt, never a payment observation. A failed call is
  recorded with its failure outcome and carries no payments.
* A payment the caller matched to an obligation becomes an official, verified
  `SourceObservation` ONLY when the call ran in `producao` with the owner's
  `PROVIDER_REAL_DATA_APPROVED`. Fake, trial and homologation results keep their
  records and never verify anything.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from re import fullmatch
from typing import Final
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.clients.models import ClientCompany
from apps.integrations.call_models import CallState, ProviderCall
from apps.obligations.models import (
    FederalConsultation,
    FederalConsultationKind,
    FederalConsultationOutcome,
    FederalEnvironment,
    FederalPayment,
    FederalPaymentType,
    Obligation,
    ObservationKind,
    ObservationSourceKind,
    ObservationVerificationStatus,
    SourceObservation,
)
from apps.obligations.models.observations import ObservationOutcome
from apps.obligations.observations import assert_fact, lock_subjects

CENTS: Final = Decimal("0.01")
MAX_AMOUNT: Final = Decimal("999999999999.99")
FAILURE_OUTCOMES: Final = frozenset(
    {
        FederalConsultationOutcome.SEM_PROCURACAO,
        FederalConsultationOutcome.FALHA,
        FederalConsultationOutcome.COTA_ESGOTADA,
        FederalConsultationOutcome.INCERTO,
    }
)


class FederalRecordConflictError(ValidationError):
    """A replay disagrees with what the official source already reported."""


@dataclass(frozen=True, slots=True)
class PaymentRecord:
    """One normalized arrecadacao document, as the Receita reported it."""

    numero_documento: str
    tipo: str
    periodo_apuracao: date
    data_arrecadacao: date
    data_vencimento: date | None
    receita_codigo: str
    receita_descricao: str
    valor_total: Decimal
    valor_principal: Decimal | None = None
    valor_multa: Decimal | None = None
    valor_juros: Decimal | None = None
    matched_obligation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ConsultationCall:
    """The call and canonical request-scope digest a consultation answers.

    `scope_digest` is the protocol's request_digest, computed by the caller from
    the canonical query. A refused-before-dispatch receipt supplies the digest
    of that same query even though it has no provider_ref.
    """

    range_start: date
    range_end: date
    consulted_at: datetime
    environment: str
    provider_ref: str
    scope_digest: str


def amount(value: object) -> Decimal:
    """Convert a provider amount to Decimal(14,2) without binary float arithmetic.

    JSON numbers arrive as `int` or `float`; `str()` yields their shortest decimal
    spelling (76.6 -> "76.6"), which is what the provider wrote. `Decimal(76.6)`
    would carry the binary expansion 76.5999... and silently lose a cent.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        msg = "Valor monetário inválido."
        raise ValidationError(msg)
    try:
        parsed = Decimal(str(value).strip())
    except InvalidOperation as error:
        msg = "Valor monetário inválido."
        raise ValidationError(msg) from error
    # Range first: quantizing a huge finite value ("1e400") raises InvalidOperation.
    if (
        not parsed.is_finite()
        or not Decimal(0) <= parsed <= MAX_AMOUNT
        or parsed != parsed.quantize(CENTS)
    ):
        msg = "Valor monetário fora do formato de centavos."
        raise ValidationError(msg)
    return parsed.quantize(CENTS)


def _optional_amount(value: object) -> Decimal | None:
    return None if value is None else amount(value)


def _date(value: object) -> date:
    if not isinstance(value, str):
        msg = "Data do documento inválida."
        raise ValidationError(msg)
    try:
        return date.fromisoformat(value[:10])
    except ValueError as error:
        msg = "Data do documento inválida."
        raise ValidationError(msg) from error


def _period(value: object) -> date:
    """PAGTOWEB periods arrive as AAAAMM or a full date; store the month's first day."""
    text = str(value).strip()
    if len(text) == len("AAAAMM") and text.isdigit():
        text = f"{text[:4]}-{text[4:]}-01"
    return _date(text).replace(day=1)


def payment_from_provider(
    item: Mapping[str, object], *, matched_obligation_id: UUID | None = None
) -> PaymentRecord:
    """Normalize one `DocumentoArrecadacaoIC` entry; refuse anything malformed."""
    tipo = str(item.get("tipo", "")).strip().upper()
    if tipo not in FederalPaymentType.values:
        msg = "Tipo de documento de arrecadação desconhecido."
        raise ValidationError(msg)
    numero = str(item.get("numeroDocumento", "")).strip()
    receita = item.get("receita")
    receita_fields = receita if isinstance(receita, Mapping) else {}
    codigo = str(receita_fields.get("codigo", "")).strip()
    if not numero or not codigo:
        msg = "Documento de arrecadação sem número ou receita."
        raise ValidationError(msg)
    vencimento = item.get("dataVencimento")
    return PaymentRecord(
        numero_documento=numero,
        tipo=tipo,
        periodo_apuracao=_period(item.get("periodoApuracao")),
        data_arrecadacao=_date(item.get("dataArrecadacao")),
        data_vencimento=None if vencimento in {None, ""} else _date(vencimento),
        receita_codigo=codigo,
        receita_descricao=str(receita_fields.get("descricao", "")).strip()[:255],
        valor_total=amount(item.get("valorTotal")),
        valor_principal=_optional_amount(item.get("valorPrincipal")),
        valor_multa=_optional_amount(item.get("valorMulta")),
        valor_juros=_optional_amount(item.get("valorJuros")),
        matched_obligation_id=matched_obligation_id,
    )


def verifies(call: ProviderCall) -> bool:
    """Whether a call's results may become official facts (INV-R4)."""
    return (
        call.environment == FederalEnvironment.PRODUCAO
        and call.state == CallState.SUCCESS
        and call.finalized_at is not None
        and settings.PROVIDER_REAL_DATA_APPROVED is True
    )


def _resolve_call(
    client: ClientCompany, call: ConsultationCall, *, failure: str | None
) -> ProviderCall | None:
    """Lock the actual protocol receipt before any client/domain locks or writes."""
    if not call.provider_ref:
        if failure is None:
            msg = "Uma consulta concluída exige a chamada do provedor."
            raise ValidationError(msg)
        return None
    persisted = (
        ProviderCall.objects.select_for_update(no_key=True)
        .filter(pk=call.provider_ref, client_id=client.pk)
        .first()
    )
    if persisted is None:
        msg = "Chamada do provedor não encontrada para este cliente."
        raise ValidationError(msg)
    if (
        persisted.environment != call.environment
        or persisted.request_digest != call.scope_digest
        or persisted.provider != "serpro"
        or persisted.operation != "SP-PAG"
        or persisted.operation_class != "read"
    ):
        msg = "Os dados da consulta divergem da chamada do provedor."
        raise ValidationError(msg)
    succeeded = (
        persisted.state == CallState.SUCCESS and persisted.finalized_at is not None
    )
    if succeeded != (failure is None):
        msg = "O resultado da consulta diverge da finalização da chamada."
        raise ValidationError(msg)
    return persisted


def _same_payment(row: FederalPayment, record: PaymentRecord) -> bool:
    return (
        row.tipo,
        row.periodo_apuracao,
        row.data_arrecadacao,
        row.data_vencimento,
        row.receita_codigo,
        row.valor_total,
        row.valor_principal,
        row.valor_multa,
        row.valor_juros,
        row.matched_obligation_id,
    ) == (
        record.tipo,
        record.periodo_apuracao,
        record.data_arrecadacao,
        record.data_vencimento,
        record.receita_codigo,
        record.valor_total,
        record.valor_principal,
        record.valor_multa,
        record.valor_juros,
        record.matched_obligation_id,
    )


def _check_matches(
    client: ClientCompany, records: Sequence[PaymentRecord]
) -> dict[UUID, Obligation]:
    """Lock every matched obligation and refuse a period the payment does not answer."""
    wanted = sorted(
        {r.matched_obligation_id for r in records if r.matched_obligation_id},
    )
    subjects = {row.pk: row for row in lock_subjects(wanted, client_id=client.pk)}
    for record in records:
        if record.matched_obligation_id is None:
            continue
        subject = subjects.get(record.matched_obligation_id)
        if subject is None or subject.competence_month != record.periodo_apuracao:
            msg = "O pagamento não corresponde à competência da obrigação."
            raise ValidationError(msg)
    return subjects


def _observe(
    consultation: FederalConsultation,
    payment: FederalPayment,
    subject: Obligation,
) -> SourceObservation:
    return assert_fact(
        tenant_id=consultation.tenant_id,
        client_id=consultation.client_id,
        subject_id=subject.pk,
        kind=ObservationKind.PAYMENT,
        period=subject.competence_month,
        source_kind=ObservationSourceKind.OFFICIAL_API,
        observed_at=consultation.consulted_at,
        value={
            "consultation_id": str(consultation.pk),
            "payment_id": str(payment.pk),
            "tipo": payment.tipo,
        },
        verification_status=ObservationVerificationStatus.VERIFIED,
        outcome=ObservationOutcome.ACCEPTED,
        source_ref=payment.numero_documento,
    )


def record_payment_consultation(
    client: ClientCompany,
    call: ConsultationCall,
    payments: Sequence[PaymentRecord] = (),
    *,
    failure: str | None = None,
) -> FederalConsultation:
    """Record one payments consultation, its documents and production-only facts.

    Must run inside the client's tenant context. A replay of the same provider call
    returns the receipt it wrote. A document number already on file must agree with
    what was reported before; a disagreement is refused with no writes.
    """
    if failure is not None and (failure not in FAILURE_OUTCOMES or payments):
        msg = "Uma consulta com falha não traz pagamentos."
        raise ValidationError(msg)
    if call.environment not in FederalEnvironment.values:
        msg = "Ambiente do provedor desconhecido."
        raise ValidationError(msg)
    if fullmatch("[a-f0-9]{64}", call.scope_digest) is None:
        msg = "O escopo da consulta exige um resumo SHA-256."
        raise ValidationError(msg)
    outcome = failure or (
        FederalConsultationOutcome.ENCONTRADO
        if payments
        else FederalConsultationOutcome.NADA_LOCALIZADO
    )
    with transaction.atomic():
        persisted = _resolve_call(client, call, failure=failure)
        locked = ClientCompany.objects.select_for_update(no_key=True).get(pk=client.pk)
        subjects = _check_matches(locked, payments)
        if call.provider_ref:
            existing = FederalConsultation.objects.filter(
                client_id=locked.pk,
                kind=FederalConsultationKind.PAGAMENTOS,
                provider_ref=call.provider_ref,
            ).first()
            if existing is not None:
                return existing
        consultation = FederalConsultation.objects.create(
            tenant_id=locked.tenant_id,
            client=locked,
            kind=FederalConsultationKind.PAGAMENTOS,
            range_start=call.range_start,
            range_end=call.range_end,
            scope_digest=call.scope_digest,
            consulted_at=(
                persisted.finalized_at
                if persisted is not None and persisted.finalized_at is not None
                else call.consulted_at
            ),
            outcome=outcome,
            provider_ref=call.provider_ref,
            environment=persisted.environment
            if persisted is not None
            else call.environment,
        )
        on_file = {
            row.numero_documento: row
            for row in FederalPayment.objects.filter(
                client_id=locked.pk,
                numero_documento__in=[r.numero_documento for r in payments],
            )
        }
        for record in payments:
            payment = on_file.get(record.numero_documento)
            if payment is None:
                payment = FederalPayment.objects.create(
                    tenant_id=locked.tenant_id,
                    client=locked,
                    consultation=consultation,
                    numero_documento=record.numero_documento,
                    tipo=record.tipo,
                    periodo_apuracao=record.periodo_apuracao,
                    data_arrecadacao=record.data_arrecadacao,
                    data_vencimento=record.data_vencimento,
                    receita_codigo=record.receita_codigo,
                    receita_descricao=record.receita_descricao,
                    valor_total=record.valor_total,
                    valor_principal=record.valor_principal,
                    valor_multa=record.valor_multa,
                    valor_juros=record.valor_juros,
                    matched_obligation_id=record.matched_obligation_id,
                )
                on_file[record.numero_documento] = payment
            elif not _same_payment(payment, record):
                msg = "O documento já registrado diverge do informado pela Receita."
                raise FederalRecordConflictError(msg)
            if (
                record.matched_obligation_id is not None
                and persisted is not None
                and verifies(persisted)
            ):
                _observe(consultation, payment, subjects[record.matched_obligation_id])
        return consultation
