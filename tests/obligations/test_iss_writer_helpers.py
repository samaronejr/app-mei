"""Exercise retained-original preparation and projection without live activation."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from django.db import transaction
from django.utils import timezone

from apps.audit.services import ObjectRef, record_event
from apps.core.tenancy import tenant_context
from apps.cycles.extraction.v2 import extract
from apps.cycles.models import DocumentIntake
from apps.obligations import fiscal_ingest
from apps.obligations.iss_retention_evidence_v1 import ISSDecision
from apps.obligations.iss_retention_incidents import ISSResolution
from apps.obligations.models import FiscalDocument, RevisionArtifact
from tests.obligations import test_fiscal_documents as fiscal_tests
from tests.obligations.fiscal_fixtures import nfse

if TYPE_CHECKING:
    from apps.cycles.extraction.v2 import FiscalExtraction
    from apps.obligations.models import Document
    from tests.portal.request_fixtures import PortalSetup

pytestmark = pytest.mark.django_db(transaction=True)
setup = fiscal_tests.setup
STATED = Decimal("12.34")


def _seed(setup: PortalSetup) -> FiscalDocument:
    record = fiscal_tests.ingest(
        setup,
        version=1,
        payload=nfse(),
        client_role="tomador",
        source_namespace="focus:nfse_recebidas",
    )
    assert isinstance(record, FiscalDocument)
    assert (record.iss_withheld_flag, record.iss_withheld_amount) == (True, STATED)
    return record


def _prepare_inputs(
    fiscal: FiscalDocument,
) -> tuple[fiscal_ingest.FiscalSource, Document, FiscalExtraction]:
    revision = fiscal_tests.revision_of(fiscal)
    artifact = RevisionArtifact.objects.select_related("document").get(
        revision=revision
    )
    original = artifact.document
    intake = DocumentIntake.objects.get(document=original)
    context = fiscal_ingest.FiscalSource(
        tenant_id=fiscal.tenant_id,
        client_id=fiscal.client_id,
        source="captura",
        provider="focus",
        provider_version=revision.provider_version,
        source_namespace=revision.source_namespace,
        environment=fiscal.environment,
        client_role=fiscal.client_role,
        receipt_ref=revision.receipt_ref,
        actor=None,
        capture_binding_id=None,
    )
    return context, original, extract(intake, nfse())


def test_prepare_reads_only_eligible_national_originals_outside_transaction(
    setup: PortalSetup,
) -> None:
    prepare = getattr(fiscal_ingest, "_prepare_iss_originals", None)
    assert callable(prepare), "Original preparation is missing from the parent."
    fiscal = _seed(setup)
    with tenant_context(fiscal.tenant_id):
        context, original, extraction = _prepare_inputs(fiscal)
    prepared = prepare(context, original, extraction)
    assert len(prepared) == 1
    assert {witness.document_id for witness in prepared} == {original.pk}
    assert list(prepared.values()) == [ISSDecision(code="2")]
    with patch.object(fiscal_ingest, "revision_iss_originals") as select:
        assert prepare(context, original, replace(extraction, family="nfe")) == {}
        select.assert_not_called()
    refusal = ""
    with (
        patch.object(
            fiscal_ingest,
            "revision_iss_originals",
            wraps=fiscal_ingest.revision_iss_originals,
        ) as select,
        transaction.atomic(),
    ):
        try:
            prepare(context, original, extraction)
        except RuntimeError as error:
            refusal = str(error)
    assert select.call_count == 0, "Preparation must refuse before selecting originals."
    assert "database transaction" in refusal


def test_prepare_keeps_attached_and_incoming_but_skips_unusable(
    setup: PortalSetup,
) -> None:
    prepare = getattr(fiscal_ingest, "_prepare_iss_originals", None)
    assert callable(prepare), "Original preparation is missing from the parent."
    fiscal = _seed(setup)
    no_code = nfse().replace(b"<tpRetISSQN>2</tpRetISSQN>", b"")
    eligible = nfse().replace(b"<tpRetISSQN>", b"\n<tpRetISSQN>")
    assert no_code != nfse()
    assert eligible != nfse()
    with tenant_context(fiscal.tenant_id):
        context, attached, _ = _prepare_inputs(fiscal)
    with patch.object(fiscal_ingest, "scanner_from_settings", fiscal_tests.FakeScanner):
        incomplete = fiscal_ingest.prepare_fiscal_document(
            context, payload_bytes=no_code, summary=None
        )
        incoming = fiscal_ingest.prepare_fiscal_document(
            context, payload_bytes=eligible, summary=None
        )
    assert incomplete is not None
    assert incoming is not None
    distinct = replace(incomplete.extraction, document_key="different-key")
    prepared = prepare(context, incomplete.original, distinct)
    assert {witness.document_id for witness in prepared} == {incomplete.original.pk}
    assert list(prepared.values()) == [ISSDecision()]
    prepared = prepare(context, incomplete.original, incomplete.extraction)
    assert {witness.document_id for witness in prepared} == {
        attached.pk,
        incomplete.original.pk,
    }
    assert all(
        prepared[witness] == ISSDecision()
        for witness in prepared
        if witness.document_id == incomplete.original.pk
    )
    union = prepare(context, incoming.original, incoming.extraction)
    assert {witness.document_id for witness in union} == {
        attached.pk,
        incoming.original.pk,
    }


def test_current_resolution_rejects_a_stale_snapshot(setup: PortalSetup) -> None:
    resolve = getattr(fiscal_ingest, "_current_iss_resolution", None)
    assert callable(resolve), "Retained-original reconciliation is missing."
    fiscal = _seed(setup)
    revision = fiscal_tests.revision_of(fiscal)
    with tenant_context(fiscal.tenant_id):
        inputs = _prepare_inputs(fiscal)
    prepared = fiscal_ingest._prepare_iss_originals(*inputs)
    with tenant_context(fiscal.tenant_id):
        assert resolve(fiscal, revision, prepared) == ISSResolution(
            "autorizada", ISSDecision(code="2")
        )
        stale = False
        try:
            resolve(fiscal, revision, {})
        except fiscal_ingest.StaleISSOriginalsError:
            stale = True
        assert stale
        fiscal.family = "nfe"
        with patch.object(fiscal_ingest, "revision_iss_originals") as select:
            assert resolve(fiscal, revision, {}) == ISSResolution(
                "autorizada", ISSDecision()
            )
            select.assert_not_called()


@pytest.mark.parametrize(
    ("old", "resolution", "owner", "expected"),
    [
        pytest.param(
            ("incerta", True, STATED),
            ("autorizada", "", False),
            True,
            True,
            id="owned-recovery",
        ),
        pytest.param(
            ("autorizada", True, STATED),
            ("autorizada", "", False),
            True,
            True,
            id="owned-attachment",
        ),
        pytest.param(
            ("autorizada", True, STATED),
            ("autorizada", "", False),
            True,
            True,
            id="owned-healthy-replay",
        ),
        pytest.param(
            ("incerta", None, None),
            ("incerta", "", True),
            True,
            False,
            id="not-restored",
        ),
        pytest.param(
            ("incerta", None, None),
            ("incerta", "evidence_unavailable", True),
            True,
            False,
            id="unavailable",
        ),
        pytest.param(
            ("incerta", None, None),
            ("autorizada", "", False),
            False,
            False,
            id="no-derive",
        ),
        pytest.param(
            ("incerta", None, None),
            ("autorizada", "", True),
            False,
            True,
            id="unfinished",
        ),
        pytest.param(
            ("incerta", True, None),
            ("autorizada", "", True),
            False,
            False,
            id="legacy-flag",
        ),
        pytest.param(
            ("incerta", None, STATED),
            ("autorizada", "", True),
            False,
            False,
            id="legacy-amount",
        ),
        pytest.param(
            ("autorizada", None, None),
            ("autorizada", "", True),
            False,
            False,
            id="legacy-attachment",
        ),
    ],
)
def test_old_iss_values_change_only_with_projection_authority(
    setup: PortalSetup,
    old: tuple[str, bool | None, Decimal | None],
    resolution: tuple[str, str, bool],
    owner: bool,
    expected: bool,
) -> None:
    may_change = getattr(fiscal_ingest, "iss_old_fields_may_change", None)
    assert callable(may_change), "The parent has no old-column write authority."
    fiscal = _seed(setup)
    revision = fiscal_tests.revision_of(fiscal)
    fiscal.fiscal_state, fiscal.iss_withheld_flag, fiscal.iss_withheld_amount = old
    state, cause, derive = resolution
    decision = ISSResolution(state, ISSDecision(cause=cause), derive)
    with tenant_context(fiscal.tenant_id):
        # Latest by append order, although timestamped before the old promotion.
        earlier = timezone.now() - timedelta(days=1)
        with patch("django.utils.timezone.now", return_value=earlier):
            record_event(
                action="fiscal.head_changed",
                tenant_id=fiscal.tenant_id,
                obj=ObjectRef("FiscalRevision", str(revision.pk)),
                metadata={"iss_projection_owned": True} if owner else {},
            )
        assert may_change(fiscal, revision, decision) is expected


def test_projection_keeps_legacy_tuple_without_old_column_authority() -> None:
    project = getattr(fiscal_ingest, "_set_iss_projection", None)
    assert callable(project), "The parent has no split projection update."
    fiscal = FiscalDocument(
        iss_retention_party="tomador",
        iss_stated=STATED,
        iss_withheld_flag=True,
        iss_withheld_amount=STATED,
    )
    assert project(fiscal, None, new_projection=False) == ["iss_retention_party"]
    assert (fiscal.iss_withheld_flag, fiscal.iss_withheld_amount) == (True, STATED)
    assert fiscal.iss_retention_party == "desconhecido"
    assert set(project(fiscal, None, new_projection=True)) == {
        "iss_withheld_flag",
        "iss_withheld_amount",
    }
    assert project(fiscal, None, new_projection=True) == []
    project(fiscal, "3", new_projection=True)
    assert (fiscal.iss_retention_party, fiscal.iss_withheld_amount) == (
        "intermediario",
        STATED,
    )
