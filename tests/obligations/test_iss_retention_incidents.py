"""Exercise incident support without activating it in the fiscal writer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib import import_module
from importlib.util import find_spec
from typing import TYPE_CHECKING
from unittest.mock import patch
from uuid import UUID

import pytest

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
    fiscal: FiscalDocument, version: str, observed_at: datetime
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
    fiscal.current_revision = revision
    fiscal.fiscal_state = revision.fiscal_state
    fiscal.save(update_fields=["current_revision", "fiscal_state"])
    record_event(
        action="fiscal.head_changed",
        obj=ObjectRef("FiscalRevision", str(revision.pk)),
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
    "new_head", [False, True], ids=["already-existed", "created-after"]
)
def test_unscoped_contradiction_only_blocks_revisions_alive_at_the_time(
    fiscal: FiscalDocument, new_head: bool
) -> None:
    support = _support()
    with tenant_context(fiscal.tenant_id):
        old = _promote(fiscal, "1", _LEGACY_EVENT_AT - timedelta(days=1))
        if not new_head:
            _record_outage(fiscal, old, derive_fields=True)

        # The accepted revision isn't named in the historical audit shape.
        with patch("django.utils.timezone.now", return_value=_LEGACY_EVENT_AT):
            legacy = record_event(
                action=_CONSISTENCY_INCIDENT,
                obj=_fiscal_ref(fiscal),
                metadata={"original_document_id": "rejected-original"},
            )
        fiscal.fiscal_state = "incerta"
        fiscal.save(update_fields=["fiscal_state"])

        head = old
        if new_head:
            head = _promote(fiscal, "2", _LEGACY_EVENT_AT + timedelta(days=1))
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
        assert Event.objects.filter(
            action="fiscal.iss_recovery_blocked",
            object_id=str(fiscal.pk),
            metadata__revision_id=str(head.pk),
        ).count() == (0 if new_head else 1)


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
