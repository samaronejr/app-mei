"""Either recovery path converges, without re-deriving a legacy tuple."""

from __future__ import annotations

from contextlib import nullcontext
from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.core.tenancy import tenant_context
from apps.obligations import fiscal_ingest
from apps.obligations import iss_retention_evidence_v1 as evidence
from apps.obligations.iss_retention_incidents import reconcile_iss_incident
from apps.obligations.models import FiscalDocument, RevisionArtifact
from tests.obligations import test_fiscal_documents
from tests.obligations.fiscal_fixtures import (
    KEY,
    LEGACY_ISS_DIGEST,
    nfse_with_retention,
    retention_element,
)
from tests.obligations.iss_backfill_fixtures import (
    ADD_ISS_PARTY,
    _ingest_retention_element,
    _ingest_retention_note,
    at_0029,
    migration_role,
    run_iss_catch_up,
    seed_legacy_code_two_heads,
)
from tests.obligations.iss_vault_fixture import fault_iss_original
from tests.obligations.iss_writer_fixtures import old_writer_events, post_scan_503
from tests.obligations.test_fiscal_documents import (
    _incidents,
    _iss_projection,
    _source_document,
    document,
    revision_of,
)

if TYPE_CHECKING:
    from apps.obligations.iss_retention_incidents import (
        ISSOutageIdentity,
        ISSResolution,
    )
    from apps.obligations.models import FiscalRevision
    from tests.portal.request_fixtures import PortalSetup

pytestmark = pytest.mark.django_db(transaction=True)
setup = test_fiscal_documents.setup


def _start_fresh_outage(setup: PortalSetup) -> FiscalDocument:
    element = retention_element("2")
    with post_scan_503(nfse_with_retention(element)):
        initial = _ingest_retention_element(setup, element)
    assert initial.fiscal_state == "incerta"
    assert _iss_projection(initial) == ("desconhecido", None, None)
    return initial


@pytest.mark.parametrize("maintenance_first", [True, False], ids=["catch-up", "live"])
def test_iss_new_projection_recovers_in_either_order(
    setup: PortalSetup, maintenance_first: bool
) -> None:
    element = retention_element("2")
    initial = _start_fresh_outage(setup)
    if maintenance_first:
        with migration_role():
            run_iss_catch_up()
        recovered = document(setup)
    else:
        recovered = _ingest_retention_element(setup, element)
        with migration_role():
            run_iss_catch_up()
    replay = _ingest_retention_element(setup, element)
    assert (
        _iss_projection(recovered)
        == _iss_projection(replay)
        == (
            "tomador",
            True,
            Decimal("12.34"),
        )
    )
    assert replay.fiscal_state == recovered.fiscal_state == "autorizada"
    assert replay.current_revision_id == initial.current_revision_id
    assert replay.version == recovered.version


@pytest.mark.parametrize("maintenance_first", [True, False], ids=["catch-up", "live"])
def test_iss_legacy_projection_survives_an_outage_in_either_order(
    setup: PortalSetup, maintenance_first: bool
) -> None:
    element = retention_element(" 2 ")
    with old_writer_events():
        initial = _ingest_retention_element(setup, element)
    with tenant_context(initial.tenant_id):
        FiscalDocument.objects.filter(pk=initial.pk).update(
            iss_withheld_flag=True, iss_withheld_amount=Decimal("12.34")
        )
        source = _source_document(initial)
    with (
        migration_role(),
        at_0029() as after,
        fault_iss_original("503", source.storage_key, nfse_with_retention(element)),
    ):
        MigrationExecutor(connection).migrate(after)
    assert document(setup).fiscal_state == "incerta"
    if maintenance_first:
        with migration_role():
            run_iss_catch_up()
    recovered = _ingest_retention_element(setup, element)
    with migration_role():
        run_iss_catch_up()
    replay = _ingest_retention_element(setup, element)
    assert (
        _iss_projection(recovered)
        == _iss_projection(replay)
        == (
            "desconhecido",
            True,
            Decimal("12.34"),
        )
    )
    assert replay.fiscal_state == "autorizada"
    assert replay.version == recovered.version


def _write_stale_attachment_marker(
    fiscal: FiscalDocument,
    revision: FiscalRevision,
    decision: evidence.ISSDecision,
    *,
    evidence_key: str | ISSOutageIdentity,
    derive_on_recovery: bool = False,
) -> ISSResolution:
    # Reproduce the old writer's ownership claim through the real outage audit.
    return reconcile_iss_incident(
        fiscal, revision, decision, evidence_key=evidence_key, derive_on_recovery=True
    )


@pytest.mark.parametrize(
    ("first", "stale_marker"),
    [
        pytest.param("catch-up", False, id="catch-up-first"),
        pytest.param("live", False, id="live-first"),
        pytest.param("migration", False, id="migration-first"),
        pytest.param("catch-up", True, id="catch-up-stale-marker"),
    ],
)
def test_iss_legacy_attachment_recovers_in_either_order(
    setup: PortalSetup, first: str, stale_marker: bool
) -> None:
    with migration_role(), at_0029() as after:
        (fiscal_id,) = seed_legacy_code_two_heads(setup, (KEY,))
        MigrationExecutor(connection).migrate(after)
    legacy = document(setup)
    revision = revision_of(legacy)
    assert legacy.pk == fiscal_id
    assert revision.semantic_digest == LEGACY_ISS_DIGEST["2"]
    with tenant_context(legacy.tenant_id):
        original = _source_document(legacy)
    if first == "migration":
        with migration_role():
            MigrationExecutor(connection).migrate([ADD_ISS_PARTY])

    marker = (
        patch.object(
            fiscal_ingest, "reconcile_iss_incident", _write_stale_attachment_marker
        )
        if stale_marker
        else nullcontext()
    )
    padded = retention_element(" 2 ")
    expected = ("desconhecido", True, Decimal("12.34"))
    with (
        marker,
        fault_iss_original(
            "503", original.storage_key, nfse_with_retention(retention_element("2"))
        ),
    ):
        attached = _ingest_retention_element(setup, padded)
    assert (attached.fiscal_state, _iss_projection(attached)) == ("incerta", expected)
    with tenant_context(legacy.tenant_id):
        assert (
            RevisionArtifact.objects.filter(
                revision=revision, role="xml_original"
            ).count()
            == 2
        )

    if first == "live":
        _ingest_retention_element(setup, padded)
    else:
        with migration_role():
            if first == "migration":
                MigrationExecutor(connection).migrate(after)
            else:
                run_iss_catch_up()
    recovered = document(setup)
    assert (recovered.fiscal_state, _iss_projection(recovered)) == (
        "autorizada",
        expected,
    )

    if first == "live":
        with migration_role():
            run_iss_catch_up()
    replay = _ingest_retention_element(setup, padded)
    assert (replay.fiscal_state, _iss_projection(replay)) == ("autorizada", expected)
    assert recovered.current_revision_id == replay.current_revision_id == revision.pk
    assert recovered.version == replay.version
    assert revision_of(replay).semantic_digest == LEGACY_ISS_DIGEST["2"]


def test_iss_catch_up_keeps_a_rollback_writer_contradiction(setup: PortalSetup) -> None:
    accepted = _start_fresh_outage(setup)

    with old_writer_events():
        conflicting = _ingest_retention_note(setup, "3")
    assert _incidents(conflicting) == ["evidence_unavailable", None]
    with migration_role():
        run_iss_catch_up()
    caught_up = document(setup)
    assert caught_up.fiscal_state == "incerta"
    assert _iss_projection(caught_up) == ("desconhecido", None, None)
    assert caught_up.current_revision_id == accepted.current_revision_id
    assert _ingest_retention_note(setup, "2").version == caught_up.version


def test_iss_catch_up_refreshes_an_owned_projection_without_a_repeat_incident(
    setup: PortalSetup,
) -> None:
    original = _ingest_retention_note(setup, "2")
    assert _iss_projection(original) == ("tomador", True, Decimal("12.34"))
    with tenant_context(original.tenant_id):
        retained = _source_document(original)
    padded = retention_element(" 2 ")
    with fault_iss_original(
        "503", retained.storage_key, nfse_with_retention(retention_element("2"))
    ):
        attached = _ingest_retention_element(setup, padded)
        assert attached.fiscal_state == "incerta"
        with migration_role():
            run_iss_catch_up()
        assert _incidents(document(setup)) == ["evidence_unavailable"]
    with migration_role():
        run_iss_catch_up()
    recovered = document(setup)
    assert recovered.fiscal_state == "autorizada"
    assert _iss_projection(recovered) == ("desconhecido", None, None)
    assert recovered.current_revision_id == original.current_revision_id
    assert _ingest_retention_element(setup, padded).version == recovered.version
