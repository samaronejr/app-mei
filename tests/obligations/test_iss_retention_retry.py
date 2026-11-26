"""ISS audit identity is independent of intervening contradictions."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from apps.audit.models import Event
from apps.core.tenancy import tenant_context
from apps.obligations.models import FiscalDocument, RevisionArtifact
from tests.obligations import test_fiscal_documents
from tests.obligations.fiscal_fixtures import nfse_with_retention, retention_element
from tests.obligations.iss_vault_fixture import fault_iss_original
from tests.obligations.iss_writer_fixtures import old_writer_events, post_scan_503
from tests.obligations.test_fiscal_documents import (
    _STATED,
    _incidents,
    _ingest_iss,
    _iss_projection,
    _source_document,
    ingest,
)

if TYPE_CHECKING:
    from tests.portal.request_fixtures import PortalSetup

pytestmark = pytest.mark.django_db(transaction=True)
setup = test_fiscal_documents.setup


def test_iss_incident_retry_ignores_intervening_hard_incident(
    setup: PortalSetup,
) -> None:
    _ingest_iss(setup, "1")
    accepted = _ingest_iss(setup, "2", version=2)
    with tenant_context(accepted.tenant_id):
        original = _source_document(accepted)
    with fault_iss_original(
        "503", original.storage_key, nfse_with_retention(retention_element("2"))
    ):
        first = _ingest_iss(setup, "3", version=2)
        assert _incidents(first) == ["evidence_disagrees", "evidence_unavailable"]
        older = ingest(
            setup,
            payload=nfse_with_retention(retention_element("3")).replace(
                b"12.34", b"99.99"
            ),
            version=1,
            client_role="tomador",
            source_namespace="focus:nfse_recebidas",
        )
        assert older is not None
        before = _incidents(first)
        assert before == [
            "evidence_disagrees",
            "evidence_unavailable",
            "evidence_disagrees",
        ]
        repeated = _ingest_iss(setup, "3", version=2)
        assert _incidents(repeated) == before
    restored = _ingest_iss(setup, "2", version=2)
    assert restored.fiscal_state == "incerta"
    assert restored.current_revision_id == accepted.current_revision_id
    assert _incidents(restored) == before


def test_iss_outage_records_a_changed_original_set_once(setup: PortalSetup) -> None:
    element = retention_element("2")
    accepted = _ingest_iss(setup, element)
    with tenant_context(accepted.tenant_id):
        original = _source_document(accepted)
    second = b'<tpRetISSQN xmlns="http://www.sped.fazenda.gov.br/nfse">2</tpRetISSQN>'
    with fault_iss_original("503", original.storage_key, nfse_with_retention(element)):
        unavailable = _ingest_iss(setup, element)
        assert _incidents(unavailable) == ["evidence_unavailable"]
        expanded = _ingest_iss(setup, second)
        assert expanded.current_revision_id == accepted.current_revision_id
        assert _incidents(expanded) == ["evidence_unavailable"] * 2
        replay = _ingest_iss(setup, second)
        assert replay.version == expanded.version
        assert _incidents(replay) == ["evidence_unavailable"] * 2


def test_iss_rollback_writer_hard_event_blocks_accepted_byte_recovery(
    setup: PortalSetup,
) -> None:
    element = retention_element("2")
    with post_scan_503(nfse_with_retention(element)):
        accepted = _ingest_iss(setup, element)
    assert accepted.fiscal_state == "incerta"

    with old_writer_events():
        conflict = _ingest_iss(setup, "3")
    assert conflict.fiscal_state == "incerta"
    assert _incidents(conflict) == ["evidence_unavailable", None]

    for _ in range(2):
        replay = _ingest_iss(setup, element)
        assert replay.fiscal_state == "incerta"
        assert replay.current_revision_id == accepted.current_revision_id
        assert _iss_projection(replay) == ("desconhecido", None, None)
    with tenant_context(accepted.tenant_id):
        assert (
            Event.objects.filter(
                object_id=str(accepted.pk),
                action="fiscal.iss_recovery_blocked",
                metadata__reason="legacy_incident_without_revision",
            ).count()
            == 1
        )


def test_iss_repromoted_revision_records_a_repeated_hard_candidate_in_its_episode(
    setup: PortalSetup,
) -> None:
    element = retention_element("2")
    payload = nfse_with_retention(element)
    accepted = _ingest_iss(setup, element)
    assert _ingest_iss(setup, "3").fiscal_state == "incerta"
    other = ingest(
        setup,
        payload=payload.replace(b">2</tpRetISSQN>", b"> 2 </tpRetISSQN>"),
        version=1,
        client_role="tomador",
        source_namespace="focus:another_nfse_stream",
    )
    assert isinstance(other, FiscalDocument)
    assert other.current_revision_id != accepted.current_revision_id
    with tenant_context(accepted.tenant_id):
        original = _source_document(accepted)
    with fault_iss_original("503", original.storage_key, payload):
        assert _ingest_iss(setup, element).fiscal_state == "incerta"
        _ingest_iss(setup, "3")
    replay = _ingest_iss(setup, element)
    assert replay.fiscal_state == "incerta"
    assert replay.current_revision_id == accepted.current_revision_id
    assert _incidents(replay) == [
        "evidence_disagrees",
        "evidence_unavailable",
        "evidence_disagrees",
    ]


def _legacy_iss_head(setup: PortalSetup) -> FiscalDocument:
    with old_writer_events():
        fiscal = _ingest_iss(setup, "2")
    # Persist the same tuple the old code-2 writer had after applying 0030.
    assert _iss_projection(fiscal)[1:] == (True, _STATED)
    with tenant_context(fiscal.tenant_id):
        promotion = Event.objects.get(
            action="fiscal.head_changed", object_id=str(fiscal.current_revision_id)
        )
        assert "iss_projection_owned" not in promotion.metadata
        FiscalDocument.objects.filter(pk=fiscal.pk).update(
            iss_retention_party="desconhecido"
        )
        fiscal.refresh_from_db()
    return fiscal


def test_iss_padded_attachment_during_outage_never_owns_legacy_values(
    setup: PortalSetup,
) -> None:
    accepted = _legacy_iss_head(setup)
    padded = retention_element(" 2 ")
    with tenant_context(accepted.tenant_id):
        original = _source_document(accepted)
    with fault_iss_original(
        "503", original.storage_key, nfse_with_retention(retention_element("2"))
    ):
        attached = _ingest_iss(setup, padded)
    assert attached.fiscal_state == "incerta"
    assert _iss_projection(attached) == ("desconhecido", True, _STATED)
    assert attached.current_revision_id == accepted.current_revision_id
    with tenant_context(accepted.tenant_id):
        assert (
            RevisionArtifact.objects.filter(
                revision_id=accepted.current_revision_id
            ).count()
            == 2
        )

    recovered = _ingest_iss(setup, padded)
    assert recovered.fiscal_state == "autorizada"
    assert _iss_projection(recovered) == ("desconhecido", True, _STATED)
    with tenant_context(accepted.tenant_id):
        outage = Event.objects.get(
            object_id=str(accepted.pk),
            action="fiscal.consistency_incident",
            metadata__cause="evidence_unavailable",
        )
        assert outage.metadata["derive_fields"] is False
    assert _ingest_iss(setup, padded).version == recovered.version
