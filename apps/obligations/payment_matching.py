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
"Pagamentos encontrados (nao associados)". A document already on file keeps the
association it was first recorded with: payments are append-only history.
"""

from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING

from apps.clients.models import ClientCompany, TaxRegime
from apps.clients.services import claim_at, current_profiles
from apps.obligations.federal import PaymentRecord
from apps.obligations.generator import DAS_CODE
from apps.obligations.models import FederalPayment, FederalPaymentType, Obligation

if TYPE_CHECKING:
    from datetime import date
    from uuid import UUID


def match_das_payments(
    client: ClientCompany, records: Sequence[PaymentRecord]
) -> list[PaymentRecord]:
    """Return the records with `matched_obligation_id` set by the rule above.

    Constant query count per page: one read of payments on file, one of the
    client's tax timeline and one of the candidate obligations.
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
        and record.numero_documento not in on_file
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
    obligation: UUID | None
    for record in records:
        if record.numero_documento in on_file:
            obligation = on_file[record.numero_documento]
        elif (
            record.tipo == FederalPaymentType.DAS
            and record.periodo_apuracao in mei_months
        ):
            obligation = obligations.get(record.periodo_apuracao)
        else:
            obligation = None
        matched.append(replace(record, matched_obligation_id=obligation))
    return matched
