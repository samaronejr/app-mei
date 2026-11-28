"""One October cycle, real fiscal material, and a final guide awaiting adoption."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.clients.models import ClientContact
from apps.core.tenancy import tenant_context
from apps.cycles import tasks
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.evidence import EvidenceLink, EvidenceRequest, RequestState
from apps.cycles.models import PlaybookVersion, ServiceCycle
from apps.cycles.openers import REQUEST_OVERDUE, evaluate_cycle
from apps.cycles.records import Exception as CycleException
from apps.cycles.scanning import FakeScanner
from apps.cycles.services.approvals import approve, propose
from apps.cycles.services.cycle import open_cycle
from apps.cycles.services.fiscal_review import (
    input_digest,
    record_fiscal_document_review,
)
from apps.integrations.call_models import CallState, ProviderCall
from apps.integrations.serpro import pgmei
from apps.integrations.serpro.termo_state import termo_head
from apps.obligations.fiscal_ingest import ingest_fiscal_document
from apps.obligations.models import Document
from tests.fakes.provider import Reply
from tests.fakes.serpro import FakeSerpro
from tests.integrations.pgmei_support import das_reply
from tests.obligations.fiscal_fixtures import nfe

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from uuid import UUID

    from apps.cycles.fiscal_records import FiscalDocumentReview
    from apps.cycles.models import ActionProposal
    from tests.integrations.pgmei_support import GuideWorld


def review_input(w: GuideWorld) -> tuple[FiscalDocumentReview, Callable[[int], UUID]]:
    tenant = w.termo.tenant
    tenant.features = [*tenant.features, "document_capture"]
    tenant.save(update_fields=["features"])
    xml = nfe(recipient=w.termo.client.cnpj).replace(b"2026-09-15", b"2026-10-15")
    with patch(
        "apps.obligations.fiscal_ingest.scanner_from_settings",
        return_value=FakeScanner(),
    ):
        fiscal = ingest_fiscal_document(
            tenant_id=w.tenant_id,
            client_id=w.cycle.client_id,
            source="importacao_equipe",
            provider="focus",
            provider_version="1",
            payload_bytes=xml,
            summary=None,
            actor=w.owner,
            environment="fake",
            client_role="destinatario",
            source_namespace="focus:nfe_recebidas",
            receipt_ref="",
        )
    assert fiscal is not None
    with tenant_context(w.tenant_id):
        w.cycle.refresh_from_db()
        assert fiscal.current_revision_id is not None
        root = record_fiscal_document_review(
            w.owner,
            w.cycle.pk,
            fiscal.pk,
            revision_id=fiscal.current_revision_id,
            decision="rejeitado",
            input_digest=input_digest(fiscal),
            expected_version=w.cycle.version,
        )
        w.cycle.refresh_from_db()

    def correct(version: int) -> UUID:
        return record_fiscal_document_review(
            w.owner,
            root.cycle_id,
            root.document_id,
            revision_id=root.revision_id,
            decision="aceito",
            input_digest=root.input_digest,
            expected_version=version,
            expected_head_id=root.pk,
            note="Revisão do documento.",
        ).pk

    return root, correct


def pending_proposal(w: GuideWorld) -> ActionProposal:
    with tenant_context(w.tenant_id):
        contact = ClientContact.objects.create(
            tenant_id=w.tenant_id,
            client_id=w.cycle.client_id,
            name="Contato",
            email="contato@example.test",
        )
        w.cycle.refresh_from_db()
        return propose(
            w.staff,
            w.cycle.pk,
            ProposalDraft(
                ProposalInput(
                    action_kind=ActionKind.EMAIL_NOTICE,
                    step_key="notice",
                    recipient_ids=(contact.pk,),
                    subject="Documentos",
                    body="Conferir.",
                ),
                timezone.now() + timedelta(days=1),
            ),
            expected_version=w.cycle.version,
        )


def decide(w: GuideWorld, proposal: ActionProposal) -> UUID:
    return approve(w.owner, proposal.pk, expected_version=proposal.version).proposal.pk


class WorkerStoppedError(Exception):
    """Setup seam: the provider finalized; only local settlement did not run."""


@contextmanager
def final_guide(w: GuideWorld, *, refused: bool = False) -> Iterator[ProviderCall]:
    with FakeSerpro((Reply(status=400) if refused else das_reply(),)) as peer:
        with (
            patch.object(pgmei, "settle_guide", side_effect=WorkerStoppedError),
            pytest.raises(WorkerStoppedError),
        ):
            pgmei.obtain_das_guide(w.owner, w.tenant_id, w.cycle.pk, origin=peer.origin)
        with tenant_context(w.tenant_id):
            call = ProviderCall.objects.get(
                operation="SP-DAS", client_id=w.cycle.client_id
            )
            assert call.state == (CallState.FAILED if refused else CallState.SUCCESS)
            pdf = Document.objects.filter(acquisition_receipt_ref=str(call.pk)).first()
            assert (pdf is None) == refused
            assert not EvidenceLink.objects.filter(document=pdf).exists()
        yield call
        assert len(peer.requests) == 1
        with tenant_context(w.tenant_id):
            if pdf is not None:
                link = EvidenceLink.objects.get(document=pdf)
                assert link.request.cycle_id == w.cycle.pk
            assert ProviderCall.objects.filter(operation="SP-DAS").count() == 1


def overdue_exception(
    w: GuideWorld, cycle: ServiceCycle | None = None, *, federal: bool = False
) -> CycleException:
    # An already-issued guide remains due even if federal checks are now off.
    # Unflagged, provider-authority openers cannot add a second, unrelated issue.
    tenant = w.termo.tenant
    assert "federal_checks" in tenant.features
    if not federal:
        tenant.features = [f for f in tenant.features if f != "federal_checks"]
        tenant.save(update_fields=["features"])
    cycle = cycle or w.cycle
    today = timezone.localdate()
    with tenant_context(w.tenant_id):
        request = EvidenceRequest.objects.create(
            tenant=tenant,
            client_id=cycle.client_id,
            cycle=cycle,
            created_by=w.owner,
            title="Documento em atraso",
            state=RequestState.REQUESTED,
            requested_at=timezone.now(),
            due_on=today - timedelta(days=2),
        )
        evaluate_cycle(cycle, today)
        existing = CycleException.objects.get(cycle=cycle)
        assert (existing.reason_code, existing.subject_ref) == (
            REQUEST_OVERDUE,
            request.pk,
        )
        return existing


@contextmanager
def after_termo_expiry(w: GuideWorld) -> Iterator[None]:
    """Run the daily task the day after the accepted termo's vigencia ended."""
    with tenant_context(w.tenant_id):
        head = termo_head(w.link.connection_id)
        assert head is not None
        assert head.valid_until is not None
    later = head.valid_until + timedelta(days=1)
    with patch.object(tasks, "timezone", SimpleNamespace(localdate=lambda: later)):
        yield


def second_cycle(w: GuideWorld) -> ServiceCycle:
    """November's case for the same client, created after October's (larger PK)."""
    with tenant_context(w.tenant_id):
        definition = PlaybookVersion.objects.get(
            playbook__key="mei-mensal", state="published"
        )
        ref = open_cycle(
            w.owner,
            w.engagement.pk,
            date(2026, 11, 1),
            definition.pk,
            idempotency_key="join-second-cycle",
        )
        cycle = ServiceCycle.objects.get(pk=ref.id)
    assert w.cycle.pk < cycle.pk
    return cycle
