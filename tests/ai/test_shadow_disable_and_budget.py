"""Durable reservations bound every call; failures never mutate core documents."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from django.contrib import admin
from django.core.files.storage import default_storage
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.automation.admission import claim
from apps.automation.llm import FakeLLM
from apps.automation.models import AgentRun, EvalRun
from apps.automation.shadow import run_shadow
from apps.automation.types import ProviderError
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.cycles.evidence import DocumentIntake, ExtractedField
from apps.tenants.models import Tenant
from tests.ai.fixtures import single_company
from tests.cycles.document_fixtures import reviewed
from tests.portal.request_fixtures import PortalSetup

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("gate", ["flag", "global", "provider", "od05"])
def test_disabled_admission_creates_no_run(
    setup: PortalSetup, settings: SettingsWrapper, gate: str
) -> None:
    intake, _ = reviewed(setup, "recibo_cliente.pdf")
    if gate == "flag":
        Tenant.objects.filter(pk=setup.graph.tenant.pk).update(features=["cycles_r1"])
    if gate == "global":
        settings.AI_GLOBAL_DISABLED = True
    if gate == "provider":
        settings.AI_PROVIDER = ""
    if gate == "od05":
        settings.AI_PROVIDER = "openai"
        settings.AI_REAL_DATA_APPROVED = False
    assert run_shadow(setup.graph.tenant.pk, intake.pk) == "skipped"
    with tenant_context(setup.graph.tenant.pk):
        assert AgentRun.objects.count() == 0


def test_success_is_private_and_redelivery_does_not_repeat(setup: PortalSetup) -> None:
    intake = single_company(setup)
    assert run_shadow(setup.graph.tenant.pk, intake.pk) == "completed"
    assert run_shadow(setup.graph.tenant.pk, intake.pk) == "skipped"
    with tenant_context(setup.graph.tenant.pk):
        run = AgentRun.objects.get()
        assert run.document_id == intake.document_id
        assert run.client_id == setup.graph.client.pk
        assert run.cost_brl == Decimal("0.001")
        assert run.reserved_brl == Decimal("0.025")
        assert run.retention_class == "ai_derived"
        assert run.expires_at > run.created_at
        assert not hasattr(run, "prompt")
    assert not admin.site.is_registered(AgentRun)
    assert not admin.site.is_registered(EvalRun)


def test_provider_failure_retains_deterministic_result(
    setup: PortalSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    intake = single_company(setup)
    with tenant_context(setup.graph.tenant.pk):
        before = list(ExtractedField.objects.values())
        original = DocumentIntake.objects.values().get(pk=intake.pk)

    def fail(*args: object) -> None:
        message = "unavailable"
        raise ProviderError(message)

    monkeypatch.setattr(FakeLLM, "complete", fail)
    assert run_shadow(setup.graph.tenant.pk, intake.pk) == "provider_error"
    with tenant_context(setup.graph.tenant.pk):
        assert list(ExtractedField.objects.values()) == before
        assert DocumentIntake.objects.values().get(pk=intake.pk) == original
        assert AgentRun.objects.get().error_code == "unavailable"


@pytest.mark.parametrize(
    "cap",
    ["document_calls", "document_cost", "tenant_day", "tenant_cost", "global_month"],
)
def test_durable_caps_prevent_provider_calls(
    setup: PortalSetup,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    cap: str,
) -> None:
    intake = single_company(setup)
    reserved = Decimal("0.025")
    copies = 2
    if cap == "document_calls":
        reserved = Decimal(0)
    if cap == "document_cost":
        reserved = Decimal("0.05")
        copies = 1
    if cap == "tenant_day":
        settings.AI_SHADOW_TENANT_DAILY_CALLS = 1
        copies = 1
    if cap == "tenant_cost":
        settings.AI_SHADOW_TENANT_DAILY_BRL = "0.025"
        copies = 1
    if cap == "global_month":
        reserved = Decimal("50.00")
        settings.AI_SHADOW_TENANT_DAILY_BRL = "100"
        copies = 1
    with tenant_context(setup.graph.tenant.pk):
        for _ in range(copies):
            AgentRun.objects.create(
                tenant=setup.graph.tenant,
                client=setup.graph.client,
                document=None if cap == "global_month" else intake.document,
                input_hash="e" * 64
                if cap == "global_month"
                else intake.document.sha256,
                model_id="fake-v1",
                state="provider_error",
                reserved_brl=reserved,
                created_at=timezone.now(),
                expires_at=timezone.now() + timedelta(days=365),
            )

    def unexpected(*args: object) -> None:
        pytest.fail("provider called after durable budget exhausted")

    monkeypatch.setattr(FakeLLM, "complete", unexpected)
    assert run_shadow(setup.graph.tenant.pk, intake.pk) == "skipped"


def test_changed_source_bytes_are_never_sent_to_provider(
    setup: PortalSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    intake = single_company(setup)
    with default_storage.open(intake.document.storage_key, "wb") as stream:
        stream.write(b"changed since deterministic extraction")

    def unexpected(*args: object) -> None:
        pytest.fail("changed source reached provider")

    monkeypatch.setattr(FakeLLM, "complete", unexpected)
    assert run_shadow(setup.graph.tenant.pk, intake.pk) == "provider_error"
    with tenant_context(setup.graph.tenant.pk):
        assert AgentRun.objects.get().error_code == "source_integrity"


def test_provider_soft_limit_records_failure_without_refund(
    setup: PortalSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    intake = single_company(setup)

    def timeout(*args: object) -> None:
        raise SoftTimeLimitExceeded

    monkeypatch.setattr(FakeLLM, "complete", timeout)
    assert run_shadow(setup.graph.tenant.pk, intake.pk) == "provider_error"
    with tenant_context(setup.graph.tenant.pk):
        run = AgentRun.objects.get()
        assert run.error_code == "provider_timeout"
        assert run.reserved_brl == Decimal("0.025")


def test_calendar_caps_roll_at_sao_paulo_midnight(
    setup: PortalSetup, settings: SettingsWrapper
) -> None:
    intake = single_company(setup)
    before = datetime(2026, 10, 1, 2, 59, 59, tzinfo=UTC)
    after = datetime(2026, 10, 1, 3, 0, 0, tzinfo=UTC)
    settings.AI_SHADOW_TENANT_DAILY_CALLS = 1
    settings.AI_SHADOW_TENANT_DAILY_BRL = "100"
    with tenant_context(setup.graph.tenant.pk):
        AgentRun.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            input_hash="e" * 64,
            model_id="fake-v1",
            state="provider_error",
            reserved_brl=Decimal(50),
            created_at=before,
            expires_at=before + timedelta(days=365),
        )
    assert claim(setup.graph.tenant.pk, intake.pk, now=before) is None
    assert claim(setup.graph.tenant.pk, intake.pk, now=after) is not None


def test_inactive_firms_spend_still_consumes_global_budget(setup: PortalSetup) -> None:
    intake = single_company(setup)
    inactive = Tenant.objects.create(
        name="Former cohort", slug="t24-former-cohort", is_active=False
    )
    with tenant_context(inactive.pk):
        client = ClientCompany.objects.create(
            tenant=inactive, legal_name="Synthetic", cnpj="11222333000181"
        )
        AgentRun.objects.create(
            tenant=inactive,
            client=client,
            input_hash="e" * 64,
            model_id="fake-v1",
            state="provider_error",
            reserved_brl=Decimal(50),
            created_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=365),
        )
    assert run_shadow(setup.graph.tenant.pk, intake.pk) == "skipped"
    with tenant_context(setup.graph.tenant.pk):
        assert AgentRun.objects.count() == 0


def test_same_hash_under_other_client_does_not_consume_this_doc_cap(
    setup: PortalSetup,
) -> None:
    intake = single_company(setup)
    with tenant_context(setup.graph.tenant.pk):
        for _ in range(2):
            AgentRun.objects.create(
                tenant=setup.graph.tenant,
                client=setup.other_client,
                input_hash=intake.document.sha256,
                model_id="fake-v1",
                state="completed",
                reserved_brl=Decimal("0.025"),
                created_at=timezone.now(),
                expires_at=timezone.now() + timedelta(days=365),
            )
    assert run_shadow(setup.graph.tenant.pk, intake.pk) == "completed"
    with tenant_context(setup.graph.tenant.pk):
        assert AgentRun.objects.filter(client=setup.graph.client).count() == 1
