"""AXRM-008: provider problems become owned work, not source observations."""

from datetime import timedelta

import pytest
from django.apps import apps

from apps.core.tenancy import tenant_context
from apps.cycles.models import Exception as CycleException
from apps.cycles.models import ServiceCycle
from apps.cycles.openers import evaluate_tenant_exceptions
from apps.cycles.services.cycle import open_cycle
from apps.integrations.models import ClientProviderLink, ProviderConnection
from apps.obligations.models import SourceObservation
from tests.cycles.cycle_fixtures import PERIOD, cycle_setup
from tests.integrations.test_authority import TODAY, authority_api

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("days", [30, 10, 7, 1, 0, -1])
def test_certificate_expiry_opens_once_with_an_owner_and_due_date(days: int) -> None:
    authority_api()
    setup = cycle_setup()
    setup.tenant.features += ["scan_required", "document_capture"]
    setup.tenant.save(update_fields=["features"])
    with tenant_context(setup.tenant.pk):
        ref = open_cycle(
            setup.actor,
            setup.engagement.pk,
            PERIOD,
            setup.definition.pk,
            idempotency_key="authority-expiry",
        )
        cycle = ServiceCycle.objects.get(pk=ref.id)
        connection = ProviderConnection.objects.create(
            tenant=setup.tenant,
            provider="focus",
            environment="fake",
            status="ativa",
        )
        link = ClientProviderLink.objects.create(
            tenant=setup.tenant,
            client=setup.client,
            connection=connection,
            provider_company_ref="expiry",
            streams=["nfe_recebidas"],
            status="ativa",
            credential_state="ativa",
            certificate_cnpj=setup.client.cnpj,
            certificate_valid_to=TODAY + timedelta(days=days),
        )
        before = SourceObservation.objects.count()
        evaluate_tenant_exceptions(setup.tenant, today=TODAY)
        evaluate_tenant_exceptions(setup.tenant, today=TODAY)
        reason = (
            "certificado_provedor_vencido"
            if days < 0
            else "certificado_provedor_vencendo"
        )
        problem = CycleException.objects.get(
            cycle=cycle,
            reason_code=reason,
            subject_ref=link.pk,
        )
        assert problem.owner_id == setup.actor.pk
        assert problem.due_on == TODAY + timedelta(days=days)
        assert not problem.client_visible
        assert SourceObservation.objects.count() == before
        assert not apps.get_model("integrations", "IntegrationIssue").objects.exists()


def test_without_an_open_cycle_the_problem_is_an_owned_integration_issue() -> None:
    authority_api()
    setup = cycle_setup()
    setup.tenant.features += ["scan_required", "document_capture"]
    setup.tenant.save(update_fields=["features"])
    issues = apps.get_model("integrations", "IntegrationIssue").objects
    with tenant_context(setup.tenant.pk):
        connection = ProviderConnection.objects.create(
            tenant=setup.tenant,
            provider="focus",
            environment="fake",
            status="ativa",
        )
        link = ClientProviderLink.objects.create(
            tenant=setup.tenant,
            client=setup.client,
            connection=connection,
            provider_company_ref="no-cycle",
            streams=["nfsen_recebidas"],
            status="ativa",
            credential_state="ativa",
            certificate_cnpj=setup.client.cnpj,
            certificate_valid_to=TODAY + timedelta(days=10),
        )
        evaluate_tenant_exceptions(setup.tenant, today=TODAY)
        evaluate_tenant_exceptions(setup.tenant, today=TODAY)
        expiring = issues.get(reason="certificado_provedor_vencendo")
        assert expiring.link_id == link.pk
        assert expiring.due_on == TODAY + timedelta(days=10)
        assert expiring.review_on == TODAY + timedelta(days=3)
        assert expiring.owner_id is not None
        missing = issues.get(reason="autorizacao_ausente")
        assert missing.service == "captura_nfse"
        assert not CycleException.objects.exists()


@pytest.mark.parametrize(
    "change",
    [{"status": "revogada"}, {"disablement_state": "pausada_localmente"}],
)
def test_inactive_links_are_not_evaluated(change: dict[str, str]) -> None:
    authority_api()
    setup = cycle_setup()
    setup.tenant.features += ["scan_required", "document_capture"]
    setup.tenant.save(update_fields=["features"])
    with tenant_context(setup.tenant.pk):
        connection = ProviderConnection.objects.create(
            tenant=setup.tenant,
            provider="focus",
            environment="fake",
            status="ativa",
        )
        ClientProviderLink.objects.create(
            tenant=setup.tenant,
            client=setup.client,
            connection=connection,
            provider_company_ref="inactive",
            streams=["nfe_recebidas"],
            credential_state="divergente",
            certificate_valid_to=TODAY - timedelta(days=1),
            **{"status": "ativa", **change},
        )
        evaluate_tenant_exceptions(setup.tenant, today=TODAY)
        assert not apps.get_model("integrations", "IntegrationIssue").objects.exists()


@pytest.mark.parametrize(
    ("provider", "other_flags"),
    [
        ("focus", []),
        # Each provider answers only to its own flag, whatever else is on.
        ("focus", ["scan_required", "federal_checks"]),
        ("serpro", ["scan_required", "document_capture"]),
    ],
)
def test_flags_off_leave_provider_conditions_inert(
    provider: str, other_flags: list[str]
) -> None:
    authority_api()
    setup = cycle_setup()
    setup.tenant.features += other_flags
    setup.tenant.save(update_fields=["features"])
    with tenant_context(setup.tenant.pk):
        open_cycle(
            setup.actor,
            setup.engagement.pk,
            PERIOD,
            setup.definition.pk,
            idempotency_key="dark-authority",
        )
        connection = ProviderConnection.objects.create(
            tenant=setup.tenant,
            provider=provider,
            environment="fake",
            status="ativa",
        )
        ClientProviderLink.objects.create(
            tenant=setup.tenant,
            client=setup.client,
            connection=connection,
            provider_company_ref=f"dark-{provider}",
            streams=["nfe_recebidas"],
            status="ativa",
            certificate_valid_to=TODAY - timedelta(days=1),
        )
        evaluate_tenant_exceptions(setup.tenant, today=TODAY)
        assert not CycleException.objects.filter(
            reason_code__startswith="certificado_provedor"
        ).exists()
        assert not CycleException.objects.filter(
            reason_code="autorizacao_ausente"
        ).exists()
