"""Bound projection and outage attribution to the current retained originals."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from django.core.files.storage import default_storage
from django.db import connection, transaction
from django.utils import timezone

from apps.audit.models import Event
from apps.audit.services import ObjectRef, record_event
from apps.core.tenancy import tenant_context
from apps.obligations import fiscal_ingest as writer
from apps.obligations.iss_retention_evidence_v1 import EVIDENCE_UNAVAILABLE
from apps.obligations.iss_retention_incidents import iss_evidence_key
from apps.obligations.models import Document, FiscalDocument, RevisionArtifact
from tests.obligations import test_fiscal_documents as fiscal_tests
from tests.obligations.fiscal_fixtures import nfse
from tests.obligations.iss_vault_fixture import fault_iss_original
from tests.obligations.test_iss_writer_helpers import STATED, _prepare_inputs, _seed

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.portal.request_fixtures import PortalSetup

pytestmark = pytest.mark.django_db(transaction=True)
setup = fiscal_tests.setup


def _incident_keys(fiscal: FiscalDocument) -> list[str]:
    return list(
        Event.objects.filter(
            action="fiscal.consistency_incident",
            object_id=str(fiscal.pk),
            metadata__cause=EVIDENCE_UNAVAILABLE,
        ).values_list("metadata__evidence_key", flat=True)
    )


def _attach_distinct(
    fiscal: FiscalDocument, setup: PortalSetup, original: Document, payload: bytes
) -> Document:
    result = fiscal_tests.ingest(
        setup,
        version=1,
        payload=payload,
        client_role="tomador",
        source_namespace="focus:nfse_recebidas",
    )
    assert isinstance(result, FiscalDocument)
    assert result.current_revision_id == fiscal.current_revision_id
    with tenant_context(fiscal.tenant_id):
        return (
            RevisionArtifact.objects.exclude(document=original)
            .select_related("document")
            .get(revision_id=fiscal.current_revision_id)
            .document
        )


@pytest.mark.parametrize("outage", [False, True], ids=["healthy", "after-503"])
@pytest.mark.parametrize(
    "stored_code", ["2", None], ids=["already-current", "old-projection"]
)
def test_owned_replay_projects_only_the_remaining_verified_original(
    setup: PortalSetup, outage: bool, stored_code: str | None
) -> None:
    fiscal = _seed(setup)
    revision = fiscal_tests.revision_of(fiscal)
    with tenant_context(fiscal.tenant_id):
        context, original, extraction = _prepare_inputs(fiscal)
        record_event(
            action="fiscal.head_changed",
            obj=ObjectRef("FiscalRevision", str(revision.pk)),
            metadata={"iss_projection_owned": True},
        )
    writer._set_iss_projection(fiscal, stored_code, new_projection=True)
    payload = nfse().replace(b"<tpRetISSQN>", b"\n<tpRetISSQN>")
    retained = _attach_distinct(fiscal, setup, original, payload)
    if outage:
        with fault_iss_original("503", retained.storage_key, payload):
            prepared = writer._prepare_iss_originals(context, retained, extraction)
        with tenant_context(fiscal.tenant_id):
            resolution = writer._current_iss_resolution(fiscal, revision, prepared)
        assert resolution.state == "incerta"
        fiscal.fiscal_state = resolution.state

    with tenant_context(fiscal.tenant_id):
        Document.objects.filter(pk=original.pk).update(purged_at=timezone.now())
        assert RevisionArtifact.objects.filter(revision=revision).count() == 2
    for attempt in range(2):
        prepared = writer._prepare_iss_originals(context, retained, extraction)
        with tenant_context(fiscal.tenant_id):
            resolution = writer._current_iss_resolution(fiscal, revision, prepared)
            changes = writer._set_iss_projection(
                fiscal,
                resolution.decision.code,
                new_projection=writer.iss_old_fields_may_change(
                    fiscal, revision, resolution
                ),
            )
        assert resolution.state == "autorizada"
        assert (
            fiscal.iss_retention_party,
            fiscal.iss_withheld_flag,
            fiscal.iss_withheld_amount,
        ) == ("tomador", True, STATED)
        assert bool(changes) == (attempt == 0 and stored_code is None)
        fiscal.fiscal_state = resolution.state


@pytest.mark.parametrize(
    "failed_first", [True, False], ids=["failed-first", "failed-last"]
)
def test_stale_scan_cannot_record_an_outage_in_either_order(
    setup: PortalSetup, failed_first: bool
) -> None:
    fiscal = _seed(setup)
    revision = fiscal_tests.revision_of(fiscal)
    with tenant_context(fiscal.tenant_id):
        inputs = _prepare_inputs(fiscal)
    original = inputs[1]
    with fault_iss_original("503", original.storage_key, nfse()):
        incomplete = writer._prepare_iss_originals(*inputs)
        attached = _attach_distinct(
            fiscal,
            setup,
            original,
            nfse().replace(b">2</tpRetISSQN>", b"> 2 </tpRetISSQN>"),
        )
        complete = writer._prepare_iss_originals(*inputs)
    failed = next(witness for witness in complete if witness.document_id == original.pk)
    healthy = next(
        witness for witness in complete if witness.document_id == attached.pk
    )
    assert complete[failed].cause == EVIDENCE_UNAVAILABLE
    assert complete[healthy].cause is None
    order = [failed, healthy] if failed_first else [healthy, failed]
    with (
        tenant_context(fiscal.tenant_id),
        patch.object(writer, "revision_iss_originals", return_value=order),
    ):
        stale = False
        try:
            writer._current_iss_resolution(fiscal, revision, incomplete)
        except writer.StaleISSOriginalsError:
            stale = True
        assert stale, "An added original must invalidate preparation before audit."
        assert _incident_keys(fiscal) == []
        writer._current_iss_resolution(fiscal, revision, complete)
        assert _incident_keys(fiscal) == [iss_evidence_key([failed])]


def test_purge_after_preparation_invalidates_the_snapshot(setup: PortalSetup) -> None:
    fiscal = _seed(setup)
    revision = fiscal_tests.revision_of(fiscal)
    with tenant_context(fiscal.tenant_id):
        context, original, extraction = _prepare_inputs(fiscal)
    payload = nfse().replace(b"<tpRetISSQN>", b"\n<tpRetISSQN>")
    retained = _attach_distinct(fiscal, setup, original, payload)
    prepared = writer._prepare_iss_originals(context, retained, extraction)
    with tenant_context(fiscal.tenant_id):
        Document.objects.filter(pk=original.pk).update(purged_at=timezone.now())
        stale = False
        try:
            writer._current_iss_resolution(fiscal, revision, prepared)
        except writer.StaleISSOriginalsError:
            stale = True
        assert stale, "A removed original must invalidate preparation before audit."
        assert _incident_keys(fiscal) == []


@contextmanager
def _manual_transaction() -> Iterator[None]:
    transaction.set_autocommit(False)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        assert not connection.in_atomic_block
        yield
    finally:
        transaction.rollback()
        transaction.set_autocommit(True)


@pytest.mark.parametrize("entry", ["prepare", "vault"])
def test_manual_transaction_is_refused_before_selecting_or_reading(
    setup: PortalSetup, entry: str
) -> None:
    fiscal = _seed(setup)
    with tenant_context(fiscal.tenant_id):
        inputs = _prepare_inputs(fiscal)
    witness = next(iter(writer._prepare_iss_originals(*inputs)))
    refused = False
    with (
        patch.object(
            writer, "revision_iss_originals", wraps=writer.revision_iss_originals
        ) as select,
        patch.object(default_storage, "open", wraps=default_storage.open) as read,
        _manual_transaction(),
    ):
        try:
            if entry == "prepare":
                writer._prepare_iss_originals(*inputs)
            else:
                writer.verify_iss_original(witness)
        except RuntimeError as error:
            refused = "database transaction" in str(error)
    assert select.call_count == 0
    assert read.call_count == 0
    assert refused
