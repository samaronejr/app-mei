"""Provider budget and metering acceptance (AXRM-010)."""

from uuid import uuid4

import pytest
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.integrations.call_contracts import CallResult
from apps.integrations.call_models import CallState
from apps.integrations.call_observations import meter
from apps.integrations.call_protocol import reserve
from apps.integrations.models import ClientProviderLink
from apps.tenants.models import UsageMetric, UsageRecord
from tests.integrations.test_call_protocol import provider_link, request

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["provider_link"]


def test_focus_reads_do_not_meter_documents_per_request(
    provider_link: ClientProviderLink,
) -> None:
    # Given a finalized Focus read (pages and XML retrieval are not document units).
    call = reserve(provider_link.tenant_id, provider_link.pk, request())
    call.provider = "focus"
    call.operation_class = "read"
    call.billing_class = "incluido_no_plano"
    # When its receipt is metered.
    with tenant_context(provider_link.tenant_id):
        meter(
            call,
            uuid4(),
            CallResult(CallState.SUCCESS, 200, "ok", "sim"),
            timezone.now(),
        )
        # Then only captured revisions may produce document measurements.
        assert not UsageRecord.objects.filter(
            metric=UsageMetric.FOCUS_DOCUMENTOS
        ).exists()
