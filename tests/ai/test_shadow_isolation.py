"""Forced RLS, paired document identity, and content-only retention."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import IntegrityError, ProgrammingError, connection, transaction
from django.utils import timezone

from apps.automation.models import AgentRun
from apps.automation.shadow import run_shadow
from apps.automation.tasks import purge_agent_outputs
from apps.core.tenancy import tenant_context
from apps.obligations.models import Document
from apps.tenants.models import Tenant
from tests.ai.fixtures import single_company
from tests.portal.request_fixtures import PortalSetup

pytestmark = pytest.mark.django_db(transaction=True)


def test_run_cannot_pair_another_clients_document(setup: PortalSetup) -> None:
    intake = single_company(setup)
    with tenant_context(setup.graph.tenant.pk):
        with pytest.raises(IntegrityError) as caught, transaction.atomic():
            AgentRun.objects.create(
                tenant=setup.graph.tenant,
                client=setup.other_client,
                document=intake.document,
                input_hash=intake.document.sha256,
                model_id="fake-v1",
                reserved_brl=Decimal("0.025"),
                expires_at=timezone.now() + timedelta(days=365),
            )
        assert getattr(caught.value.__cause__, "sqlstate", None) == "23503"
    assert run_shadow(setup.graph.tenant.pk, intake.pk) == "completed"


def test_portal_has_no_privilege_and_other_tenant_sees_nothing(
    setup: PortalSetup,
) -> None:
    intake = single_company(setup)
    assert run_shadow(setup.graph.tenant.pk, intake.pk) == "completed"
    stranger = Tenant.objects.create(name="Another firm", slug="t24-isolated")
    with tenant_context(stranger.pk), connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM automation_agentrun")
        assert cursor.fetchone() == (0,)
    with tenant_context(setup.graph.tenant.pk), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE app_portal")
        with pytest.raises(ProgrammingError) as caught, transaction.atomic():
            cursor.execute("SELECT * FROM automation_agentrun")
        assert getattr(caught.value.__cause__, "sqlstate", None) == "42501"


def test_retention_redacts_output_even_after_flag_removed(setup: PortalSetup) -> None:
    intake = single_company(setup)
    assert run_shadow(setup.graph.tenant.pk, intake.pk) == "completed"
    Tenant.objects.filter(pk=setup.graph.tenant.pk).update(features=[])
    with tenant_context(setup.graph.tenant.pk):
        AgentRun.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    assert purge_agent_outputs() == 1
    with tenant_context(setup.graph.tenant.pk):
        row = AgentRun.objects.get()
        assert row.output == {}
        assert row.reserved_brl == Decimal("0.025")


def test_source_deletion_erases_content_not_budget_metadata(setup: PortalSetup) -> None:
    with tenant_context(setup.graph.tenant.pk):
        document = Document.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            sha256="a" * 64,
            uploaded_by=setup.graph.actor,
            byte_size=1,
            original_filename="synthetic",
            content_type="text/plain",
        )
        run = AgentRun.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            document=document,
            input_hash=document.sha256,
            model_id="fake-v1",
            output={"fields": ["private"]},
            reserved_brl=Decimal("0.025"),
            expires_at=timezone.now() + timedelta(days=365),
        )
        document.delete()
        run.refresh_from_db()
        assert run.output == {}
        assert run.document_id is None
        assert run.reserved_brl == Decimal("0.025")
