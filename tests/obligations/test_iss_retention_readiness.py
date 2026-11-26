"""A completed label belongs to one current snapshot, not a peer or stale head."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.audit.models import Event
from apps.audit.services import ObjectRef, record_event
from apps.core.tenancy import tenant_context
from apps.cycles.evidence import ExtractedField
from apps.obligations.iss_retention_evidence_v1 import revision_iss_originals
from apps.obligations.iss_retention_readiness import _stamp
from apps.obligations.models import FiscalDocument
from tests.obligations import test_fiscal_documents
from tests.obligations.fiscal_fixtures import KEY
from tests.obligations.iss_backfill_fixtures import (
    ADD_ISS_PARTY,
    _ingest_retention_note,
    at_0029,
    migration_role,
    run_iss_catch_up,
    seed_legacy_code_two_heads,
)
from tests.obligations.test_fiscal_documents import _source_document, document
from tests.obligations.test_iss_backfill_review import (
    _BACKFILL,
    _assert_pending,
    _review_body,
)
from tests.portal.request_fixtures import PortalSetup, portal_setup

pytestmark = pytest.mark.django_db(transaction=True)
setup = test_fiscal_documents.setup


def test_iss_review_readiness_cannot_be_borrowed_and_is_idempotent(
    setup: PortalSetup, settings: SettingsWrapper
) -> None:
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    foreign = portal_setup("iss-readiness-foreign")
    browser = setup.firm()
    tenant, client = setup.graph.tenant.pk, setup.graph.client.pk
    action = "fiscal.iss_party_ready"
    with migration_role(), at_0029():
        (head_id,) = seed_legacy_code_two_heads(setup, [KEY])
        MigrationExecutor(connection).migrate([ADD_ISS_PARTY])
        ref = ObjectRef("FiscalDocument", str(head_id))
        with tenant_context(tenant):
            fiscal = FiscalDocument.objects.get(pk=head_id)
            fiscal.iss_retention_party = "tomador"
            fiscal.save(update_fields=["iss_retention_party"])
            originals = revision_iss_originals(
                connection, (tenant, client, fiscal.current_revision_id)
            )
            stamp = _stamp(fiscal, originals)
            # Keep the head and snapshot exact, changing only the client scope.
            record_event(
                action=action,
                obj=ref,
                metadata={**stamp, "client_id": str(setup.other_client.pk)},
            )
        record_event(
            action=action, obj=ref, tenant_id=foreign.graph.tenant.pk, metadata=stamp
        )
        _assert_pending(_review_body(browser, head_id))

        MigrationExecutor(connection).migrate([_BACKFILL])
        assert "ISS retido pelo tomador" in _review_body(browser, head_id)
        assert run_iss_catch_up() == "ISS parties updated: 0\n"
        with tenant_context(tenant):
            assert (
                Event.objects.filter(
                    action=action, object_id=ref.id, metadata=stamp
                ).count()
                == 1
            )

    url = reverse("document-review", args=[head_id])
    assert setup.firm(setup.graph.staff).get(url).status_code == 404
    assert foreign.firm().get(url).status_code == 404


def test_iss_review_waits_when_same_head_evidence_was_redacted(
    setup: PortalSetup, settings: SettingsWrapper
) -> None:
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    browser = setup.firm()
    fiscal = _ingest_retention_note(setup, "2")
    assert "ISS retido pelo tomador" in _review_body(browser, fiscal.pk)
    with tenant_context(fiscal.tenant_id):
        # Match the retention service's field redaction, without a fiscal write.
        original = _source_document(fiscal)
        assert (
            ExtractedField.objects.filter(
                document=original, name="iss_withheld_flag"
            ).update(value="", source_span="", redacted_at=fiscal.created_at)
            == 1
        )
    stale = document(setup)
    assert (stale.current_revision_id, stale.version, stale.iss_retention_party) == (
        fiscal.current_revision_id,
        fiscal.version,
        "tomador",
    )
    _assert_pending(_review_body(browser, fiscal.pk))

    with migration_role():
        run_iss_catch_up()
    assert "Retenção do ISS desconhecida" in _review_body(browser, fiscal.pk)
