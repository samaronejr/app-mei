"""Regressions for legal migration order, ISS recovery scope and staff exposure."""

from uuid import UUID

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.audit.models import Event
from apps.core.tenancy import tenant_context
from apps.cycles.models import CycleState, ServiceCycle
from apps.integrations.models import IntegrationIssue
from apps.obligations.models import FiscalDocument
from tests.obligations import test_fiscal_documents
from tests.obligations.fiscal_fixtures import KEY, nfe, retention_element
from tests.obligations.iss_backfill_fixtures import (
    ADD_ISS_PARTY,
    BEFORE_ISS_PARTY,
    _ingest_retention_note,
    _payload,
    at_0029,
    migration_role,
    run_iss_catch_up,
    seed_legacy_code_two_heads,
)
from tests.obligations.iss_vault_fixture import fault_iss_original
from tests.obligations.iss_writer_fixtures import old_writer_events, post_scan_503
from tests.obligations.test_fiscal_documents import (
    _STATED,
    _source_document,
    document,
    ingest,
)
from tests.portal.request_fixtures import PortalSetup

pytestmark = pytest.mark.django_db(transaction=True)
setup = test_fiscal_documents.setup

_BACKFILL = ("obligations", "0031_backfill_iss_retention_party")
_BEFORE_INTEGRATION_ISSUES = ("integrations", "0001_initial")
_INTEGRATION_ISSUES = ("integrations", "0002_authority_records")
_AUDIT_ORDER = ("audit", "0008_fiscal_event_order")
_BEFORE_AUDIT_ORDER = ("audit", "0007_datasubjectrequest_notification_bookkeeping")
_INCIDENT = "fiscal.consistency_incident"
_REASON = "incidente_consistencia_fiscal"
_PENDING = "Retenção do ISS ainda não disponível"
_LABELS = (
    "ISS não retido",
    "ISS retido pelo tomador",
    "ISS retido pelo intermediário",
    "Retenção do ISS desconhecida",
)


def test_iss_migration_orders_and_opens_the_no_cycle_issue(setup: PortalSetup) -> None:
    tenant, client = setup.graph.tenant.pk, setup.graph.client.pk
    with tenant_context(tenant):
        assert (
            ServiceCycle.objects.filter(client_id=client).update(
                state=CycleState.CANCELLED
            )
            == 1
        )

    with migration_role(), at_0029():
        (head_id,) = seed_legacy_code_two_heads(setup, [KEY])
        MigrationExecutor(connection).migrate([ADD_ISS_PARTY])
        MigrationExecutor(connection).migrate([_BEFORE_INTEGRATION_ISSUES])
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('integrations_integrationissue')")
            assert cursor.fetchone() == (None,)

        executor = MigrationExecutor(connection)
        plan = executor.loader.graph.forwards_plan(_BACKFILL)
        assert _INTEGRATION_ISSUES in plan[: plan.index(_BACKFILL)]
        with tenant_context(tenant):
            original = _source_document(FiscalDocument.objects.get(pk=head_id))
        payload = _payload(retention_element("2"))
        with fault_iss_original("503", original.storage_key, payload):
            executor.migrate([_BACKFILL])

        with tenant_context(tenant):
            issue = IntegrationIssue.objects.get(client_id=client, reason=_REASON)
            assert issue.owner_id == setup.graph.actor.pk
            assert issue.evidence_refs == [str(head_id)]
            head = FiscalDocument.objects.get(pk=head_id)
            assert (head.fiscal_state, head.iss_retention_party) == (
                "incerta",
                "desconhecido",
            )


@pytest.mark.parametrize("entry_point", ["migration", "catch-up"])
def test_iss_maintenance_leaves_legacy_nfe_conflict_unchanged(
    setup: PortalSetup, entry_point: str
) -> None:
    ingest(setup, payload=nfe())
    with old_writer_events():
        ingest(setup, payload=nfe(total="1200.00"))
    conflict = document(setup)
    assert (conflict.family, conflict.fiscal_state) == ("nfe", "incerta")
    tenant = conflict.tenant_id
    with migration_role():
        executor = MigrationExecutor(connection)
        try:
            # Drop the order column after the real same-version conflict was
            # recorded with the legacy INSERT shape, then reapply it.
            executor.migrate([_BEFORE_AUDIT_ORDER])
            MigrationExecutor(connection).migrate([_AUDIT_ORDER])
            with tenant_context(tenant):
                events = Event.objects.filter(
                    object_type="FiscalDocument", object_id=str(conflict.pk)
                )
                historic = events.get(action=_INCIDENT)
                assert historic.fiscal_order is None
                retained = _source_document(conflict)
                assert set(historic.metadata) == {"original_document_id"}
                assert historic.metadata["original_document_id"] != str(retained.pk)

            ingest(setup, payload=nfe())  # Live replay does not append ISS history.
            with tenant_context(tenant):
                assert list(events.values_list("pk", "action")) == [
                    (historic.pk, _INCIDENT)
                ]
            if entry_point == "migration":
                MigrationExecutor(connection).migrate([_BACKFILL])
            else:
                assert run_iss_catch_up() == "ISS parties updated: 0\n"
            with tenant_context(tenant):
                actual = list(events.values_list("pk", "action"))
            assert actual == [(historic.pk, _INCIDENT)]
        finally:
            MigrationExecutor(connection).migrate(executor.loader.graph.leaf_nodes())

    current = document(setup)
    assert (current.current_revision_id, current.fiscal_state, current.version) == (
        conflict.current_revision_id,
        "incerta",
        conflict.version,
    )


def _review_body(browser: Client, pk: UUID) -> str:
    response = browser.get(reverse("document-review", args=[pk]))
    assert response.status_code == 200
    return response.content.decode()


def _assert_pending(body: str) -> None:
    exposed = [label for label in _LABELS if label in body]
    assert exposed == [], "A default or stale ISS label was rendered before completion"
    assert _PENDING in body


def test_iss_review_waits_for_legacy_backfill(
    setup: PortalSetup, settings: SettingsWrapper
) -> None:
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    browser = setup.firm()
    with migration_role(), at_0029():
        (head_id,) = seed_legacy_code_two_heads(setup, [KEY])
        MigrationExecutor(connection).migrate([ADD_ISS_PARTY])
        _assert_pending(_review_body(browser, head_id))
        MigrationExecutor(connection).migrate([_BACKFILL])
        body = _review_body(browser, head_id)
    assert "ISS retido pelo tomador" in body
    assert _PENDING not in body


def test_iss_review_waits_for_catch_up_after_an_old_head_advance(
    setup: PortalSetup, settings: SettingsWrapper
) -> None:
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    browser = setup.firm()
    initial = _ingest_retention_note(setup, "1")
    assert "ISS não retido" in _review_body(browser, initial.pk)
    latest = _ingest_retention_note(setup, "3", version=2)
    old_writer = (
        MigrationExecutor(connection)
        .loader.project_state([BEFORE_ISS_PARTY])
        .apps.get_model("obligations", "FiscalDocument")
    )
    with tenant_context(initial.tenant_id):
        FiscalDocument.objects.filter(pk=initial.pk).update(
            current_revision_id=initial.current_revision_id,
            iss_retention_party="nao_retido",
            iss_withheld_flag=False,
            iss_withheld_amount=None,
        )
        row = old_writer._base_manager.get(pk=initial.pk)
        row.current_revision_id = latest.current_revision_id
        row.iss_withheld_flag, row.iss_withheld_amount = True, _STATED
        row.version += 1
        row.save()
        assert document(setup).iss_retention_party == "nao_retido"

    _assert_pending(_review_body(browser, initial.pk))
    with migration_role():
        run_iss_catch_up()
    body = _review_body(browser, initial.pk)
    assert "ISS retido pelo intermediário" in body
    assert "ISS não retido" not in body
    assert _PENDING not in body


def test_iss_review_does_not_certify_a_retained_read_outage(
    setup: PortalSetup, settings: SettingsWrapper
) -> None:
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    browser = setup.firm()
    with post_scan_503(_payload(retention_element("3"))):
        initial = _ingest_retention_note(setup, "3")
    assert initial.fiscal_state == "incerta"
    _assert_pending(_review_body(browser, initial.pk))

    _ingest_retention_note(setup, "3")  # Complete the same head via live replay.
    assert "ISS retido pelo intermediário" in _review_body(browser, initial.pk)
