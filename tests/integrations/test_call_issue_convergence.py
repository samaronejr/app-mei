"""Uncertain calls converge on AXRM-008's single active issue (AXRM-009 placement)."""

from datetime import timedelta
from hashlib import sha256

import pytest
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.integrations.call_contracts import CallRequest, Operation, register_operation
from apps.integrations.call_models import CallState, ProviderCall
from apps.integrations.call_protocol import claim, reserve
from apps.integrations.models import (
    ClientProviderLink,
    IntegrationIssue,
    IssueReason,
    IssueState,
)
from apps.tenants.models import Membership
from tests.integrations.test_call_protocol import (
    SyntheticPolicy,
    provider_link,
    reload_call,
    request,
)

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["provider_link"]


def quarantine(link: ClientProviderLink, call_request: CallRequest) -> ProviderCall:
    row = reserve(link.tenant_id, link.pk, call_request)
    assert claim(link.tenant_id, row.pk) is not None
    with tenant_context(link.tenant_id):
        ProviderCall.objects.filter(pk=row.pk).update(
            lease_until=timezone.now() - timedelta(seconds=1)
        )
    assert claim(link.tenant_id, row.pk) is None
    assert reload_call(link, row.pk).state == CallState.UNCERTAIN
    return row


def uncertainty_issues(link: ClientProviderLink) -> list[tuple[str, str, list[str]]]:
    with tenant_context(link.tenant_id):
        return list(
            IntegrationIssue.objects.filter(reason=IssueReason.CHAMADA_PROVEDOR_INCERTA)
            .order_by("service")
            .values_list("service", "state", "evidence_refs")
        )


def test_issue_in_treatment_absorbs_a_new_uncertain_call(
    provider_link: ClientProviderLink,
) -> None:
    # Given an uncertain call whose owned issue staff already took up.
    first = quarantine(provider_link, request("first"))
    with tenant_context(provider_link.tenant_id):
        IntegrationIssue.objects.filter(
            reason=IssueReason.CHAMADA_PROVEDOR_INCERTA
        ).update(state=IssueState.EM_TRATAMENTO)
    # When another call of the same operation becomes uncertain.
    second = quarantine(provider_link, request("second"))
    # Then the issue being handled lists both calls; no second active row appears.
    assert uncertainty_issues(provider_link) == [
        ("SP-DAS", IssueState.EM_TRATAMENTO, [str(first.pk), str(second.pk)])
    ]


def test_open_issue_keeps_every_uncertain_call_discoverable(
    provider_link: ClientProviderLink,
) -> None:
    # Given two calls of one operation that both end uncertain.
    first = quarantine(provider_link, request("first"))
    second = quarantine(provider_link, request("second"))
    # Then one open issue names both calls, in the order they became uncertain.
    assert uncertainty_issues(provider_link) == [
        ("SP-DAS", IssueState.ABERTA, [str(first.pk), str(second.pk)])
    ]


def test_each_operation_keeps_its_own_uncertainty_issue(
    provider_link: ClientProviderLink,
) -> None:
    # Given a second registered SERPRO operation for the same client.
    register_operation(
        Operation(
            code="SP-SIT-P",
            provider="serpro",
            revision="v1",
            operation_class="issuance",
            feature="federal_checks",
            billing_class="nao_faturada",
            policy=SyntheticPolicy(),
        )
    )
    sitfis = CallRequest(
        operation="SP-SIT-P",
        request_key=sha256(b"sitfis-key").hexdigest(),
        request_digest=sha256(b"sitfis-body").hexdigest(),
        business_effect_key=sha256(b"sitfis-effect").hexdigest(),
    )
    # When a call of each operation becomes uncertain.
    das = quarantine(provider_link, request("das"))
    protocol = quarantine(provider_link, sitfis)
    # Then the exact A6 operation separates the two owned issues.
    assert uncertainty_issues(provider_link) == [
        ("SP-DAS", IssueState.ABERTA, [str(das.pk)]),
        ("SP-SIT-P", IssueState.ABERTA, [str(protocol.pk)]),
    ]


def test_operation_codes_must_fit_the_uncertainty_issue_service(
    provider_link: ClientProviderLink,
) -> None:
    # Given a code longer than the uncertainty issue's service column (16), which
    # would otherwise fail the quarantine write inside finalize.
    unknown = Operation(
        code="SP-OPERACAO-LONGA",
        provider="serpro",
        revision="v1",
        operation_class="read",
        feature="federal_checks",
        billing_class="consulta",
        policy=SyntheticPolicy(),
    )
    # When an adapter tries to register it, then the registry refuses it.
    with pytest.raises(ValueError, match="cannot be registered"):
        register_operation(unknown)


def test_uncertain_call_without_any_owner_stays_quarantined_without_an_issue(
    provider_link: ClientProviderLink,
) -> None:
    # Given a firm whose only owner left and a client without a responsible.
    Membership.objects.filter(tenant_id=provider_link.tenant_id).update(is_active=False)
    # When a call becomes uncertain, then it is still quarantined, deliberately
    # without an unowned work item (AXRM-008 never opens an issue without owner).
    row = quarantine(provider_link, request("orphan"))
    assert claim(provider_link.tenant_id, row.pk) is None
    assert uncertainty_issues(provider_link) == []


def test_claim_alone_quarantines_an_expired_lease_with_owned_work(
    provider_link: ClientProviderLink,
) -> None:
    # Given a claimed call whose worker died before ever reaching dispatch.
    row = reserve(provider_link.tenant_id, provider_link.pk, request("crashed"))
    assert claim(provider_link.tenant_id, row.pk) is not None
    with tenant_context(provider_link.tenant_id):
        ProviderCall.objects.filter(pk=row.pk).update(
            lease_until=timezone.now() - timedelta(seconds=1)
        )
    # When only a later claim sweep finds it (no dispatch attempt follows).
    assert claim(provider_link.tenant_id, row.pk) is None
    # Then the sweep itself quarantines it and opens the owned work item.
    assert reload_call(provider_link, row.pk).state == CallState.UNCERTAIN
    assert uncertainty_issues(provider_link) == [
        ("SP-DAS", IssueState.ABERTA, [str(row.pk)])
    ]
