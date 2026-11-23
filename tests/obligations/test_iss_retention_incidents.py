"""Exercise incident support without activating it in the fiscal writer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib import import_module
from importlib.util import find_spec
from typing import TYPE_CHECKING
from unittest.mock import patch
from uuid import UUID

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.audit.models import Event
from apps.audit.services import ObjectRef, record_event
from apps.core.tenancy import tenant_context
from apps.obligations.iss_retention_evidence_v1 import (
    EVIDENCE_DISAGREES,
    EVIDENCE_UNAVAILABLE,
    ISSDecision,
    ISSOriginal,
)
from apps.obligations.models import FiscalDocument, FiscalRevision
from tests.integrations.test_migration_rollback import _as_migrator
from tests.portal.request_fixtures import portal_setup

if TYPE_CHECKING:
    from types import ModuleType

pytestmark = pytest.mark.django_db(transaction=True)

_LEGACY_EVENT_AT = datetime(2026, 9, 2, 12, tzinfo=UTC)
_UNAVAILABLE = ISSDecision(cause=EVIDENCE_UNAVAILABLE)
_VERIFIED = ISSDecision("2")
_CONSISTENCY_INCIDENT = "fiscal.consistency_incident"
_SUPPORT = "apps.obligations.iss_retention_incidents"


def _support() -> ModuleType:
    assert find_spec(_SUPPORT) is not None, "Incident episode support is not installed."
    return import_module(_SUPPORT)


@pytest.fixture
def fiscal() -> FiscalDocument:
    setup = portal_setup("iss-incident-scope")
    tenant, client = setup.graph.tenant, setup.graph.client
    with tenant_context(tenant.pk):
        return FiscalDocument.objects.create(
            tenant=tenant,
            client=client,
            family="nfse_nacional",
            environment="fake",
            key_namespace="nacional",
            document_key="incident-proof",
            source="captura",
            provider="focus",
            client_role="tomador",
            fiscal_state="autorizada",
        )


def _fiscal_ref(fiscal: FiscalDocument) -> ObjectRef:
    return ObjectRef("FiscalDocument", str(fiscal.pk))


def _promote(
    fiscal: FiscalDocument,
    version: str,
    observed_at: datetime,
    *,
    registered: bool = True,
) -> FiscalRevision:
    revision = FiscalRevision.objects.create(
        tenant_id=fiscal.tenant_id,
        client_id=fiscal.client_id,
        document=fiscal,
        source_namespace="focus:nfse_recebidas",
        provider_version=version,
        semantic_digest="2" * 64,
        observed_at=observed_at,
        fiscal_state="autorizada",
    )
    ref = ObjectRef("FiscalRevision", str(revision.pk))
    if registered:
        record_event(
            action="fiscal.revision_registered",
            obj=ref,
            metadata={"document_id": str(fiscal.pk)},
        )
    fiscal.current_revision = revision
    fiscal.fiscal_state = revision.fiscal_state
    fiscal.save(update_fields=["current_revision", "fiscal_state"])
    record_event(
        action="fiscal.head_changed",
        obj=ref,
        metadata={"document_id": str(fiscal.pk)},
    )
    return revision


def _record_outage(
    fiscal: FiscalDocument,
    revision: FiscalRevision,
    evidence_key: str = "first",
    *,
    derive_fields: bool = False,
) -> None:
    resolution = _support().reconcile_iss_incident(
        fiscal,
        revision,
        _UNAVAILABLE,
        evidence_key=evidence_key,
        derive_on_recovery=derive_fields,
    )
    assert (resolution.state, resolution.decision) == ("incerta", _UNAVAILABLE)
    fiscal.fiscal_state = resolution.state
    fiscal.save(update_fields=["fiscal_state"])


@pytest.mark.parametrize(
    ("case", "new_head", "legacy_offset", "registered"),
    [
        ("already-existed", False, 2, True),
        ("created-after", True, -2, True),
        ("backward-clock", False, -1, True),
        ("forward-skew", True, 10, True),
        ("missing-registration", False, -1, False),
    ],
    ids=[
        "already-existed",
        "created-after",
        "backward-clock",
        "forward-skew",
        "missing-registration",
    ],
)
def test_unscoped_contradiction_only_blocks_revisions_alive_at_the_time(
    fiscal: FiscalDocument,
    case: str,
    new_head: bool,
    legacy_offset: int,
    registered: bool,
) -> None:
    support = _support()
    at = _LEGACY_EVENT_AT
    with tenant_context(fiscal.tenant_id):
        with patch("django.utils.timezone.now", return_value=at):
            old = _promote(fiscal, "1", at, registered=registered)
        if not new_head:
            with patch(
                "django.utils.timezone.now", return_value=at + timedelta(seconds=1)
            ):
                _record_outage(fiscal, old, derive_fields=True)

        # The accepted revision isn't named in the historical audit shape.
        stamp = at + timedelta(seconds=legacy_offset)
        # A rollback process can also mint a wall-clock-derived UUID whose
        # sort position disagrees with insertion order.
        legacy_pk = (
            UUID("00000000-0001-7000-8000-000000000000")
            if not new_head
            else UUID("eeeeeeee-eeee-7000-8000-000000000000")
        )
        with (
            patch("django.utils.timezone.now", return_value=stamp),
            patch.object(Event._meta.pk, "_get_default", lambda: legacy_pk),
        ):
            legacy = record_event(
                action=_CONSISTENCY_INCIDENT,
                obj=_fiscal_ref(fiscal),
                metadata={"original_document_id": "rejected-original"},
            )
        fiscal.fiscal_state = "incerta"
        fiscal.save(update_fields=["fiscal_state"])

        head = old
        if new_head:
            with patch(
                "django.utils.timezone.now", return_value=at + timedelta(seconds=4)
            ):
                head = _promote(fiscal, "2", at + timedelta(seconds=4))
            with patch(
                "django.utils.timezone.now", return_value=at + timedelta(seconds=5)
            ):
                _record_outage(fiscal, head, derive_fields=True)
        resolution = support.reconcile_iss_incident(
            fiscal, head, _VERIFIED, evidence_key=""
        )
        assert (resolution.state, resolution.decision, resolution.derive_fields) == (
            ("autorizada", _VERIFIED, True)
            if new_head
            else ("incerta", ISSDecision(cause=EVIDENCE_DISAGREES), False)
        )
        assert Event.objects.filter(pk=legacy.pk).exists()
        blocked = Event.objects.filter(
            action="fiscal.iss_recovery_blocked",
            object_id=str(fiscal.pk),
            metadata__revision_id=str(head.pk),
        )
        assert blocked.count() == (0 if new_head else 1), case
        if not new_head:
            assert blocked.get().metadata["reason"] == (
                "legacy_incident_without_revision"
                if registered
                else "legacy_incident_order_unknown"
            )


def test_pre_sequence_legacy_incident_is_retained_and_blocks_recovery(
    fiscal: FiscalDocument,
) -> None:
    before = ("audit", "0007_datasubjectrequest_notification_bookkeeping")
    with _as_migrator():
        executor = MigrationExecutor(connection)
        assert [
            (m.app_label, m.name, reverse)
            for m, reverse in executor.migration_plan([before])
        ] == [("audit", "0008_fiscal_event_order", True)]
        executor.migrate([before])
        old_event = executor.loader.project_state([before]).apps.get_model(
            "audit", "Event"
        )
        # _as_migrator restores the installed schema on context exit.
        with tenant_context(fiscal.tenant_id):
            historic = old_event._base_manager.create(
                tenant_id=fiscal.tenant_id,
                action=_CONSISTENCY_INCIDENT,
                object_type="FiscalDocument",
                object_id=str(fiscal.pk),
                metadata={"original_document_id": "historic-original"},
            )

    with tenant_context(fiscal.tenant_id):
        historic_after = Event.objects.get(pk=historic.pk)
        assert historic_after.fiscal_order is None
        assert historic_after.created_at == historic.created_at
        assert historic_after.metadata == historic.metadata
        head = _promote(fiscal, "2", _LEGACY_EVENT_AT)
        _record_outage(fiscal, head)
        # A code-only rollback uses the old INSERT shape after migration.
        rollback = old_event._base_manager.create(
            tenant_id=fiscal.tenant_id,
            action=_CONSISTENCY_INCIDENT,
            object_type="FiscalDocument",
            object_id=str(fiscal.pk),
            metadata={"original_document_id": "rollback-original"},
        )
        order = Event.objects.get(pk=rollback.pk).fiscal_order
        registration = Event.objects.get(
            action="fiscal.revision_registered", object_id=str(head.pk)
        ).fiscal_order
        assert registration is not None
        assert order is not None
        assert registration < order
        result = _support().reconcile_iss_incident(
            fiscal, head, _VERIFIED, evidence_key=""
        )
        assert result.state == "incerta"
        reason = Event.objects.get(
            action="fiscal.iss_recovery_blocked",
            object_id=str(fiscal.pk),
            metadata__revision_id=str(head.pk),
        ).metadata["reason"]
        assert reason == "legacy_incident_order_unknown"


def test_scoped_hard_incident_survives_backward_clock(fiscal: FiscalDocument) -> None:
    support = _support()
    at = _LEGACY_EVENT_AT
    with tenant_context(fiscal.tenant_id):
        with patch("django.utils.timezone.now", return_value=at):
            head = _promote(fiscal, "1", at)
        with patch("django.utils.timezone.now", return_value=at + timedelta(seconds=1)):
            _record_outage(fiscal, head)
        with patch("django.utils.timezone.now", return_value=at - timedelta(seconds=1)):
            hard = record_event(
                action=_CONSISTENCY_INCIDENT,
                obj=_fiscal_ref(fiscal),
                metadata={
                    "revision_id": str(head.pk),
                    "cause": EVIDENCE_DISAGREES,
                },
            )
        assert (
            support.incident_episode(_fiscal_ref(fiscal), str(head.pk))
            .filter(pk=hard.pk)
            .exists()
        )
        result = support.reconcile_iss_incident(
            fiscal, head, _VERIFIED, evidence_key=""
        )
        assert (result.state, result.decision) == (
            "incerta",
            ISSDecision(cause=EVIDENCE_DISAGREES),
        )


def test_repromotion_does_not_move_revision_creation_anchor(
    fiscal: FiscalDocument,
) -> None:
    support = _support()
    with tenant_context(fiscal.tenant_id):
        v1 = _promote(fiscal, "1", _LEGACY_EVENT_AT)
        record_event(
            action=_CONSISTENCY_INCIDENT,
            obj=_fiscal_ref(fiscal),
            metadata={"original_document_id": "rejected-original"},
        )
        _promote(fiscal, "2", _LEGACY_EVENT_AT)
        fiscal.current_revision = v1
        fiscal.save(update_fields=["current_revision"])
        record_event(
            action="fiscal.head_changed",
            obj=ObjectRef("FiscalRevision", str(v1.pk)),
            metadata={"document_id": str(fiscal.pk)},
        )
        _record_outage(fiscal, v1)
        result = support.reconcile_iss_incident(fiscal, v1, _VERIFIED, evidence_key="")
        assert result.state == "incerta"
        assert Event.objects.filter(
            action="fiscal.iss_recovery_blocked",
            object_id=str(fiscal.pk),
            metadata__revision_id=str(v1.pk),
            metadata__reason="legacy_incident_without_revision",
        ).exists()


def test_outage_identity_includes_original_set_not_unrelated_latest_event(
    fiscal: FiscalDocument,
) -> None:
    support = _support()
    with tenant_context(fiscal.tenant_id):
        revision = _promote(fiscal, "1", _LEGACY_EVENT_AT)
        _record_outage(fiscal, revision, "originals-one")
        record_event(
            action=_CONSISTENCY_INCIDENT,
            obj=_fiscal_ref(fiscal),
            metadata={"revision_id": str(revision.pk), "cause": EVIDENCE_DISAGREES},
        )
        _record_outage(fiscal, revision, "originals-one")
        _record_outage(fiscal, revision, "originals-two")
        _record_outage(fiscal, revision, "originals-two")
        assert list(
            support.incident_episode(_fiscal_ref(fiscal), str(revision.pk))
            .order_by("created_at", "pk")
            .values_list("metadata__cause", "metadata__evidence_key")
        ) == [
            (EVIDENCE_UNAVAILABLE, "originals-one"),
            (EVIDENCE_DISAGREES, None),
            (EVIDENCE_UNAVAILABLE, "originals-two"),
        ]
        resolution = support.reconcile_iss_incident(
            fiscal, revision, _VERIFIED, evidence_key=""
        )
        assert resolution.state == "incerta"


def test_evidence_key_is_order_independent_but_covers_original_digests() -> None:
    evidence_key = _support().iss_evidence_key
    one = ISSOriginal(UUID(int=1), "opaque1", "1" * 64, 100, "2")
    two = ISSOriginal(UUID(int=2), "opaque2", "2" * 64, 100, "2")
    key = evidence_key((one, two))
    assert key == evidence_key((two, one))
    assert key != evidence_key((one,))
    assert key != evidence_key((one, two._replace(sha256="3" * 64)))
