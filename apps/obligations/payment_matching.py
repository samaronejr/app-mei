"""The deterministic rule that binds a federal payment to a DAS-MEI obligation.

AXRM-015 (plan todo 15). A payment is matched to an obligation only when ALL hold:

1. the payment's family is DAS (document type code 09);
2. the client is MEI for the payment's `periodo_apuracao` month, asked of the
   effective-dated tax profile exactly as the DAS calendar sweep asks it
   (`apps.obligations.tasks.refresh_das_calendars`): a recorded timeline decides,
   and only a client with no recorded profile at all falls back to the legacy flag;
3. the same client has a DAS obligation whose `competence_month` equals that month.

Amounts are never compared: an unequal amount still matches and an equal amount
never creates a match. Everything else stays unassociated and is listed under
"Pagamentos encontrados (nao associados)".

A document already on file keeps an association once it has one. A document first
recorded while the rule did not hold (typically: its DAS obligation did not exist
yet) is bound when a later consultation returns it and the rule now holds. That
binding is the only change a payment row ever admits: obligations.0026's trigger
lets `matched_obligation_id` go from NULL to an obligation once, with every
reported field unchanged, and refuses any re-pointing, rewrite or delete.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from django.db import transaction

from apps.audit.services import ObjectRef, record_event
from apps.clients.models import ClientCompany, TaxRegime
from apps.clients.services import claim_at, current_profiles
from apps.obligations.generator import DAS_CODE
from apps.obligations.models import FederalPayment, FederalPaymentType, Obligation

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date
    from uuid import UUID

    from apps.obligations.federal import PaymentRecord


def _bind_on_file(
    client: ClientCompany, obligation_id: UUID, numbers: list[str]
) -> None:
    """Bind this client's still-unassociated documents to the obligation, once."""
    with transaction.atomic():
        payment_ids = list(
            FederalPayment.objects.select_for_update(no_key=True)
            .filter(
                client_id=client.pk,
                numero_documento__in=numbers,
                matched_obligation_id__isnull=True,
            )
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        FederalPayment.objects.filter(pk__in=payment_ids).update(
            matched_obligation_id=obligation_id
        )
        for payment_id in payment_ids:
            record_event(
                action="obligations.federal_payment_associated",
                tenant_id=client.tenant_id,
                obj=ObjectRef("FederalPayment", str(payment_id)),
                metadata={"obligation": str(obligation_id)},
            )


def match_das_payments(
    client: ClientCompany, records: Sequence[PaymentRecord]
) -> list[PaymentRecord]:
    """Return the records with `matched_obligation_id` set by the rule above.

    Documents on file that the rule now binds are bound here, so the caller must
    run this inside the transaction that records the page. Constant query count per
    page: one read of payments on file, one of the client's tax timeline and one of
    the candidate obligations, plus the binding writes when a later match occurs.
    """
    on_file: dict[str, UUID | None] = dict(
        FederalPayment.objects.filter(
            client_id=client.pk,
            numero_documento__in=[record.numero_documento for record in records],
        ).values_list("numero_documento", "matched_obligation_id")
    )
    months = {
        record.periodo_apuracao
        for record in records
        if record.tipo == FederalPaymentType.DAS
        and on_file.get(record.numero_documento) is None
    }
    obligations: dict[date, UUID] = {}
    mei_months: set[date] = set()
    if months:
        timeline = current_profiles([client]).get(client.pk, [])
        for month in months:
            if timeline:
                profile = claim_at(timeline, month)
                is_mei = profile is not None and profile.regime == TaxRegime.MEI
            else:
                is_mei = client.is_mei
            if is_mei:
                mei_months.add(month)
        obligations = dict(
            Obligation.objects.filter(
                client_id=client.pk,
                obligation_type_id=DAS_CODE,
                competence_month__in=mei_months,
            ).values_list("competence_month", "pk")
        )
    matched: list[PaymentRecord] = []
    late: dict[UUID, list[str]] = {}
    obligation: UUID | None
    for record in records:
        stored = on_file.get(record.numero_documento)
        if stored is not None:
            obligation = stored
        elif (
            record.tipo == FederalPaymentType.DAS
            and record.periodo_apuracao in mei_months
        ):
            obligation = obligations.get(record.periodo_apuracao)
            if obligation is not None and record.numero_documento in on_file:
                late.setdefault(obligation, []).append(record.numero_documento)
        else:
            obligation = None
        matched.append(replace(record, matched_obligation_id=obligation))
    for obligation_id, numbers in late.items():
        _bind_on_file(client, obligation_id, numbers)
    return matched
