"""Budget admission uses real concurrent PostgreSQL sessions."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
from django.db import connection, connections
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.integrations import call_contracts
from apps.integrations.budget import BudgetPolicy
from apps.integrations.budget_models import ProviderBudget
from apps.integrations.call_contracts import AdmissionDeniedError
from apps.integrations.call_models import CallState, ProviderCall
from apps.integrations.call_protocol import claim, perform, reserve
from apps.integrations.models import (
    ClientProviderLink,
    IntegrationIssue,
    ProviderConnection,
)
from apps.tenants.models import Tenant, UsageRecord
from tests.integrations.provider_peer import ProviderPeer
from tests.integrations.test_call_protocol import provider_link, request

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["provider_link"]


@pytest.fixture
def budget_link(
    provider_link: ClientProviderLink, settings: SettingsWrapper
) -> ClientProviderLink:
    settings.USAGE_UNIT_PRICES_BRL = {"serpro_consulta": "0.24"}
    settings.PROVIDER_GLOBAL_MONTHLY_CAP_BRL = {"serpro": "1.00"}
    call_contracts._OPERATIONS["SP-DAS"] = replace(
        call_contracts.operation_for("SP-DAS"),
        code="SP-PAG",
        operation_class="read",
        billing_class="consulta",
        policy=BudgetPolicy(lambda call: True),
    )
    call_contracts._OPERATIONS["SP-PAG"] = call_contracts._OPERATIONS.pop("SP-DAS")
    with tenant_context(provider_link.tenant_id):
        ProviderBudget.objects.create(
            tenant_id=provider_link.tenant_id,
            provider="serpro",
            window_start=timezone.now().date().replace(day=1),
            cap_microbrl=500_000,
            tariff_revision="synthetic-tariff-v1",
        )
    return provider_link


def paid_request(key: str = "one") -> call_contracts.CallRequest:
    return replace(request(key), operation="SP-PAG")


def test_reservation_balance_is_committed_before_claim(
    budget_link: ClientProviderLink,
) -> None:
    # Given a fresh month and a priced call.
    # When the public reservation transaction returns.
    call = reserve(budget_link.tenant_id, budget_link.pk, paid_request())
    # Then all of its maximum exposure is already included in the shared balance.
    with tenant_context(budget_link.tenant_id):
        assert (
            ProviderBudget.objects.get().reserved == call.reserved_microbrl == 240_000
        )


def test_third_reservation_refuses_before_wire_and_opens_issue(
    budget_link: ClientProviderLink,
) -> None:
    # Given two paid requests against a 500000 micro-BRL cap.
    with ProviderPeer() as peer:
        for key in ("one", "two"):
            call = reserve(budget_link.tenant_id, budget_link.pk, paid_request(key))
            ticket = claim(budget_link.tenant_id, call.pk)
            assert ticket is not None
            perform(ticket, peer)
        # When the third would bring liability to 720000.
        with pytest.raises(AdmissionDeniedError, match="cota_esgotada"):
            reserve(budget_link.tenant_id, budget_link.pk, paid_request("three"))
        # Then no third wire request exists and the refusal is owned work.
        assert len(peer.requests) == 2
    with tenant_context(budget_link.tenant_id):
        budget = ProviderBudget.objects.get()
        assert (budget.known_liability, budget.reserved) == (480_000, 0)
        assert IntegrationIssue.objects.filter(
            reason="cota_provedor_esgotada", owner__isnull=False
        ).exists()


def test_concurrent_reservations_cannot_exceed_tenant_cap(
    budget_link: ClientProviderLink,
) -> None:
    # Given only enough cap for one of two concurrent sessions.
    with tenant_context(budget_link.tenant_id):
        ProviderBudget.objects.update(cap_microbrl=240_000)
    barrier = Barrier(2, timeout=10)

    def attempt(key: str) -> str:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET ROLE app_runtime")
                cursor.execute("SET statement_timeout = '10s'")
            barrier.wait()
            try:
                reserve(budget_link.tenant_id, budget_link.pk, paid_request(key))
            except AdmissionDeniedError:
                return "refused"
            return "reserved"
        finally:
            connections.close_all()

    # When both threads contend at the public reservation boundary.
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(attempt, "one")
        second = pool.submit(attempt, "two")
        results = [first.result(timeout=15), second.result(timeout=15)]
    # Then the committed total is one unit, never two.
    assert sorted(results) == ["refused", "reserved"]
    with tenant_context(budget_link.tenant_id):
        assert ProviderBudget.objects.get().reserved == 240_000


def test_platform_cap_serializes_two_different_firms(
    budget_link: ClientProviderLink, settings: SettingsWrapper
) -> None:
    # Given two independent tenant budgets but only one platform-wide unit.
    settings.PROVIDER_GLOBAL_MONTHLY_CAP_BRL = {"serpro": "0.24"}
    other = Tenant.objects.create(
        name="Other capped firm",
        slug="other-capped",
        features=["cycles_r1", "scan_required", "federal_checks"],
    )
    with tenant_context(other.pk):
        client = ClientCompany.objects.create(
            tenant=other,
            legal_name="Other synthetic",
            cnpj="11444777000161",
        )
        provider = ProviderConnection.objects.create(
            tenant=other,
            provider="serpro",
            environment="fake",
            status="ativa",
        )
        link = ClientProviderLink.objects.create(
            tenant=other,
            client=client,
            connection=provider,
            provider_company_ref="other-company",
            status="ativa",
        )
        ProviderBudget.objects.create(
            tenant=other,
            provider="serpro",
            window_start=timezone.now().date().replace(day=1),
            cap_microbrl=500_000,
            tariff_revision="synthetic-tariff-v1",
        )
    barrier = Barrier(2, timeout=10)

    def attempt(target: ClientProviderLink) -> tuple[int, str]:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET ROLE app_runtime")
                cursor.execute("SET statement_timeout = '10s'")
                cursor.execute("SELECT pg_backend_pid()")
                pid = cursor.fetchone()[0]
            barrier.wait()
            try:
                reserve(target.tenant_id, target.pk, paid_request())
            except AdmissionDeniedError:
                return pid, "refused"
            return pid, "reserved"
        finally:
            connections.close_all()

    # When two separate PostgreSQL sessions race with different tenant row locks.
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(attempt, budget_link)
        second = pool.submit(attempt, link)
        outcomes = [first.result(timeout=15), second.result(timeout=15)]
    # Then a per-tenant lock alone cannot incorrectly admit both firms.
    assert len({pid for pid, _ in outcomes}) == 2
    assert sorted(result for _, result in outcomes) == ["refused", "reserved"]
    total = 0
    for target in (budget_link, link):
        with tenant_context(target.tenant_id):
            total += ProviderBudget.objects.get().reserved
    assert total == 240_000


def test_three_fake_billed_calls_consume_three_units(
    budget_link: ClientProviderLink,
) -> None:
    # Given sufficient authorized cap for the named happy scenario.
    with tenant_context(budget_link.tenant_id):
        ProviderBudget.objects.update(cap_microbrl=1_000_000)
    # When the wire returns 200, 202 and 403 for three distinct paid requests.
    for status in (200, 202, 403):
        call = reserve(budget_link.tenant_id, budget_link.pk, paid_request(str(status)))
        ticket = claim(budget_link.tenant_id, call.pk)
        assert ticket is not None
        with ProviderPeer(status=status) as peer:
            perform(ticket, peer)
            assert len(peer.requests) == 1
    # Then the billed denial consumes the same unit as each success.
    with tenant_context(budget_link.tenant_id):
        budget = ProviderBudget.objects.get()
        assert (budget.known_liability, budget.reserved) == (720_000, 0)
        assert UsageRecord.objects.filter(metric="serpro_consulta").count() == 3


def test_platform_cap_counts_inactive_firms_exposure(
    budget_link: ClientProviderLink, settings: SettingsWrapper
) -> None:
    # Given an inactive firm that still carries this month's SERPRO liability.
    settings.PROVIDER_GLOBAL_MONTHLY_CAP_BRL = {"serpro": "0.24"}
    inactive = Tenant.objects.create(
        name="Inactive capped firm",
        slug="inactive-capped",
        features=["cycles_r1", "scan_required", "federal_checks"],
        is_active=False,
    )
    with tenant_context(inactive.pk):
        ProviderBudget.objects.create(
            tenant=inactive,
            provider="serpro",
            window_start=timezone.now().date().replace(day=1),
            cap_microbrl=500_000,
            known_liability=240_000,
            tariff_revision="synthetic-tariff-v1",
        )
    # When an active firm asks for one more unit under the platform ceiling.
    with pytest.raises(AdmissionDeniedError, match="cota_esgotada"):
        reserve(budget_link.tenant_id, budget_link.pk, paid_request())
    # Then deactivation does not erase spend already counted platform-wide.
    with tenant_context(budget_link.tenant_id):
        assert ProviderBudget.objects.get().reserved == 0


def test_concurrent_refusals_converge_on_one_owned_quota_issue(
    budget_link: ClientProviderLink,
) -> None:
    # Given a monthly cap below a single unit.
    with tenant_context(budget_link.tenant_id):
        ProviderBudget.objects.update(cap_microbrl=100_000)
    barrier = Barrier(2, timeout=10)

    def attempt(key: str) -> str:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET ROLE app_runtime")
                cursor.execute("SET statement_timeout = '10s'")
            barrier.wait()
            try:
                reserve(budget_link.tenant_id, budget_link.pk, paid_request(key))
            except AdmissionDeniedError as error:
                return str(error)
            return "reserved"
        finally:
            connections.close_all()

    # When two sessions are refused at the same time.
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(attempt, "one")
        second = pool.submit(attempt, "two")
        results = [first.result(timeout=15), second.result(timeout=15)]
    # Then both refusals join AXRM-008's single owned issue for the operation.
    assert results == ["cota_esgotada", "cota_esgotada"]
    with tenant_context(budget_link.tenant_id):
        refused = {
            str(pk)
            for pk in ProviderCall.objects.filter(state=CallState.QUOTA).values_list(
                "pk", flat=True
            )
        }
        issues = list(
            IntegrationIssue.objects.filter(
                reason="cota_provedor_esgotada", state__in=("aberta", "em_tratamento")
            )
        )
        assert len(refused) == 2
        assert len(issues) == 1
        assert issues[0].service == "SP-PAG"
        assert issues[0].owner_id is not None
        assert set(issues[0].evidence_refs) == refused
