"""The additive storage head can read exact revision evidence without projecting it."""

from decimal import Decimal
from uuid import uuid4

import pytest
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor

from apps.core.tenancy import tenant_context
from apps.cycles.evidence import ExtractedField
from apps.obligations.iss_retention_evidence_v1 import (
    ISSDecision,
    agreed_iss_code,
    revision_iss_originals,
    verify_iss_original,
)
from apps.obligations.models import FiscalDocument
from tests.obligations import test_fiscal_documents
from tests.obligations.fiscal_fixtures import KEY, nfse
from tests.obligations.test_fiscal_documents import document, ingest, revision_of
from tests.portal.request_fixtures import PortalSetup

pytestmark = pytest.mark.django_db(transaction=True)
setup = test_fiscal_documents.setup


def test_iss_retention_repair_old_writer_can_insert_after_migration(
    setup: PortalSetup,
) -> None:
    historical = MigrationExecutor(connection).loader.project_state(
        [("obligations", "0029_fiscal_register")]
    )
    old_writer = historical.apps.get_model("obligations", "FiscalDocument")
    with tenant_context(setup.graph.tenant.pk), transaction.atomic():
        old = old_writer._base_manager.create(
            tenant_id=setup.graph.tenant.pk,
            client_id=setup.graph.client.pk,
            environment="fake",
            family="nfse_nacional",
            key_namespace="nacional",
            document_key=f"NFS{KEY}",
            client_role="tomador",
            source="captura",
            provider="focus",
        )
        assert (
            FiscalDocument.objects.get(pk=old.pk).iss_retention_party == "desconhecido"
        )


def test_iss_evidence_resolves_only_the_current_scoped_original(
    setup: PortalSetup,
) -> None:
    tenant, client = setup.graph.tenant.pk, setup.graph.client.pk
    original = nfse()
    options = {"client_role": "tomador", "source_namespace": "focus:nfse_recebidas"}
    ingest(
        setup,
        payload=original.replace(b">2</tpRetISSQN>", b">3</tpRetISSQN>"),
        version=1,
        **options,
    )
    stale = revision_of(document(setup))
    ingest(setup, payload=original, version=2, **options)
    fiscal = document(setup)
    revision = revision_of(fiscal)
    scope = (tenant, client, revision.pk)
    with tenant_context(tenant):
        assert stale.pk != revision.pk
        current = revision_iss_originals(connection, scope)
        foreign = revision_iss_originals(connection, (tenant, uuid4(), revision.pk))
        assert agreed_iss_code(foreign, verify_iss_original) == ISSDecision()
        assert len(current) == 1
        document_id = current[0].document_id
        assert document_id is not None
        ExtractedField.objects.filter(document_id=document_id).update(
            redacted_at=revision.observed_at
        )
        redacted = revision_iss_originals(connection, scope)
    assert agreed_iss_code(current, verify_iss_original) == ISSDecision("2")
    assert agreed_iss_code(redacted, verify_iss_original) == ISSDecision()
    assert fiscal.iss_withheld_flag is True
    assert fiscal.iss_withheld_amount == Decimal("12.34")
