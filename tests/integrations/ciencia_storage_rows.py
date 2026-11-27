"""Direct-insert rows for AXRM-022's private Ciencia storage (no operation, no worker).

Each builder writes one same-scope row so the storage tests can prove the database
contract (0019's reference trigger, the reverse guards) without the later services.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from django.utils import timezone

from apps.clients.models import ClientAssignment, ClientCompany
from apps.core.identifiers import uuid7
from apps.core.tenancy import tenant_context
from apps.cycles.effects.inputs import ProposalInput
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.models import ActionProposal, ProposalState, ServiceCycle
from apps.cycles.publications import ActionExecution
from apps.cycles.records import Exception as CycleException
from apps.cycles.records import ExceptionInternal
from apps.cycles.services.approvals import _intent
from apps.cycles.services.proposed_transitions import propose_waiver
from apps.integrations.call_models import CallState, ProviderCall
from apps.integrations.ciencia_models import CienciaRecord
from apps.integrations.focus.capture_wire import digest
from apps.integrations.models import (
    AuthorityRecord,
    ClientProviderLink,
    IntegrationIssue,
)
from apps.obligations.models import Document
from tests.cycles.test_fiscal_manifestation_action import _manifest
from tests.integrations.capture_fixtures import CaptureWorld, capture_world, key
from tests.integrations.ciencia_fixtures import manifestation_kind
from tests.obligations.fiscal_fixtures import summary

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path
    from uuid import UUID

    import pytest
    from pytest_django.fixtures import SettingsWrapper

    from apps.obligations.models import FiscalDocument
    from tests.cycles.cycle_fixtures import CycleSetup


@dataclass
class StorageWorld:
    """A capture graph, an assigned initiator and one received NF-e summary."""

    capture: CaptureWorld
    document: FiscalDocument

    @property
    def tenant_id(self) -> UUID:
        return self.capture.tenant_id

    @property
    def graph(self) -> CycleSetup:
        return self.capture.setup.graph


def storage_world(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> StorageWorld:
    capture = capture_world(settings, tmp_path, monkeypatch)
    graph = capture.setup.graph
    with tenant_context(capture.tenant_id):
        ClientAssignment.objects.create(
            tenant=graph.tenant, client=graph.client, user=graph.staff
        )
    document = capture.ingest(version=1, summary={**summary(), "chNFe": key(1)})
    return StorageWorld(capture, document)


def fiscal_inputs(world: StorageWorld) -> ProposalInput:
    return ProposalInput(
        action_kind=manifestation_kind(),
        step_key="ciencia",
        fiscal_document_ids=(world.document.pk,),
        provider_link_id=world.capture.link.pk,
    )


@dataclass(frozen=True)
class RowShape:
    """What a stored row claims: its kind, raw input blob, natural intent and spec."""

    kind: ActionKind | None = None
    blob: Mapping[str, object] | None = None
    intent: str | None = None
    spec: ProposalInput | None = None


def stored_row(
    graph: CycleSetup,
    cycle: ServiceCycle,
    shape: RowShape,
    state: str = ProposalState.AWAITING,
) -> ActionProposal:
    """A frozen row whose bytes name its own metadata (the cycles_frozen_proposal
    trigger), with any stored kind, input blob and natural intent.

    The database binds the row kind to its frozen manifest, never to the blob.
    """
    with tenant_context(graph.tenant.pk):
        cycle = type(cycle).objects.get(pk=cycle.pk)
        spec = shape.spec or ProposalInput(
            action_kind=manifestation_kind(),
            step_key="ciencia",
            fiscal_document_ids=(uuid4(),),
            provider_link_id=uuid4(),
        )
        kind = shape.kind or manifestation_kind()
        pk = uuid7()
        expires = (timezone.now() + timedelta(hours=72)).replace(microsecond=0)
        key = shape.intent or _intent(cycle, spec)
        vector = replace(
            _manifest(),
            action_kind=kind,
            tenant_id=str(graph.tenant.pk),
            client_id=str(graph.client.pk),
            cycle_id=str(cycle.pk),
            proposal_id=str(pk),
            proposal_revision=1,
            initiator_id=str(graph.staff.pk),
            business_intent_key=key,
            not_before=None,
            expires_at=expires,
        )
        return ActionProposal.objects.create(
            pk=pk,
            tenant=graph.tenant,
            client=graph.client,
            cycle=cycle,
            initiator=graph.staff,
            business_intent_key=key,
            action_kind=kind,
            input_spec=dict(shape.blob) if shape.blob is not None else spec.as_data(),
            manifest_bytes=vector.canonical_bytes(),
            digest=vector.digest,
            state=state,
            expires_at=expires,
            approved_at=timezone.now() if state == ProposalState.APPROVED else None,
        )


def fiscal_proposal(
    world: StorageWorld,
    state: str = ProposalState.AWAITING,
    *,
    blob: Mapping[str, object] | None = None,
    intent: str | None = None,
    kind: ActionKind | None = None,
) -> ActionProposal:
    """A stored row for this world's document (fiscal unless another kind is given)."""
    return stored_row(
        world.graph,
        world.capture.setup.cycle,
        RowShape(kind, blob, intent, fiscal_inputs(world)),
        state,
    )


def waiver_inputs(world: StorageWorld) -> ProposalInput:
    """The exact input a legacy waiver of this world's request freezes."""
    request = world.capture.setup.request
    return ProposalInput(
        action_kind=ActionKind.REQUEST_WAIVER,
        step_key=f"waive:{request.pk}",
        evidence_request_ids=(request.pk,),
        reason="Synthetic reason",
    )


def legacy_proposal(world: StorageWorld) -> ActionProposal:
    request = world.capture.setup.request
    with tenant_context(world.tenant_id):
        return propose_waiver(
            world.graph.staff,
            request.pk,
            reason="Synthetic reason",
            expected_version=request.version,
        )


def call_fields(
    world: StorageWorld,
    operation: str,
    *,
    proposal: ActionProposal | None = None,
    fiscal_revision_id: object = None,
    label: str = "row",
) -> dict[str, object]:
    link = world.capture.link
    return {
        "tenant_id": world.tenant_id,
        "client_id": link.client_id,
        "link": link,
        "operation": operation,
        "operation_revision": "v1",
        "operation_class": "write" if operation == "FC-CIE" else "read",
        "provider": "focus",
        "environment": "fake",
        "request_key": digest(f"axrm022-storage-{label}-{uuid4()}"),
        "business_effect_key": digest(f"axrm022-storage-effect-{label}-{uuid4()}"),
        "request_digest": digest(f"axrm022-storage-request-{label}"),
        "state": CallState.QUEUED,
        "connection_version": link.connection.version,
        "link_version": link.version,
        "credential_generation": link.credential_generation,
        "billing_class": "incluido_no_plano",
        "proposal": proposal,
        "fiscal_revision_id": fiscal_revision_id,
    }


def provider_call(
    world: StorageWorld,
    operation: str,
    *,
    proposal: ActionProposal | None = None,
    fiscal_revision_id: object = None,
    label: str = "row",
) -> ProviderCall:
    fields = call_fields(
        world,
        operation,
        proposal=proposal,
        fiscal_revision_id=fiscal_revision_id,
        label=label,
    )
    with tenant_context(world.tenant_id):
        return ProviderCall.objects.create(**fields)


def paired_call(world: StorageWorld, operation: str = "FC-CIE") -> ProviderCall:
    """A call bound to an exact fiscal proposal and the document's current revision."""
    proposal = fiscal_proposal(world)
    return provider_call(
        world,
        operation,
        proposal=proposal,
        fiscal_revision_id=world.document.current_revision_id,
        label=operation,
    )


def issue(world: StorageWorld) -> IntegrationIssue:
    graph = world.graph
    with tenant_context(world.tenant_id):
        return IntegrationIssue.objects.create(
            tenant=graph.tenant,
            client=graph.client,
            reason="manifestacao_conclusiva_pendente",
            service="ciencia",
            link=world.capture.link,
            owner=graph.staff,
            review_on=date(2026, 12, 15),
        )


def ciencia_record(
    world: StorageWorld, *, call: ProviderCall | None = None, **extra: object
) -> CienciaRecord:
    """An unconfirmed recovery identity: no issue, no exception, unless given."""
    call = call or paired_call(world)
    assert call.proposal_id is not None
    assert call.fiscal_revision_id is not None
    graph = world.graph
    with tenant_context(world.tenant_id):
        return CienciaRecord.objects.create(
            tenant=graph.tenant,
            client=graph.client,
            call=call,
            environment="fake",
            document_key=key(1),
            proposal_ref=call.proposal_id,
            cycle_ref=world.capture.setup.cycle.pk,
            fiscal_revision_ref=call.fiscal_revision_id,
            engagement_ref=world.capture.setup.cycle.engagement_id,
            owner=graph.staff,
            issued_on=date(2026, 9, 15),
            **extra,
        )


def followup(world: StorageWorld, documents: list[object]) -> ExceptionInternal:
    graph = world.graph
    with tenant_context(world.tenant_id):
        problem = CycleException.objects.create(
            tenant=graph.tenant,
            client=graph.client,
            cycle=world.capture.setup.cycle,
            subject_ref=uuid4(),
            reason_code="manifestacao_conclusiva_pendente",
            owner=graph.staff,
            review_on=date(2026, 12, 15),
        )
        return ExceptionInternal.objects.create(
            tenant=graph.tenant,
            client=graph.client,
            exception=problem,
            next_action="Registrar a manifestação conclusiva.",
            manifestation_documents=documents,
        )


def assessment(
    world: StorageWorld, *, annex_ii: bool, note: str, service: str = "ciencia"
) -> AuthorityRecord:
    graph = world.graph
    evidence = b"synthetic-client-ciencia-authorization"
    with tenant_context(world.tenant_id):
        document = Document.objects.create(
            tenant=graph.tenant,
            client=graph.client,
            uploaded_by=graph.actor,
            client_upload=False,
            portal_visible=False,
            acquisition_origin="upload_equipe",
            sha256=digest(evidence.decode()),
            original_filename="autorizacao-ciencia.pdf",
            content_type="application/pdf",
            byte_size=len(evidence),
        )
        return AuthorityRecord.objects.create(
            tenant=graph.tenant,
            client=graph.client,
            kind="autorizacao_ciencia"
            if service == "ciencia"
            else "autorizacao_captura",
            service=service,
            code="autorizacao_cliente",
            status="ativa",
            valid_from=world.capture.link.capture_start_month,
            evidence_document=document,
            declared_by=graph.actor,
            verified_by=graph.actor,
            conclusive_manifestation_annex_ii=annex_ii,
            conclusive_manifestation_note=note,
        )


def fiscal_execution(world: StorageWorld) -> ActionExecution:
    """A fiscal-kind execution on a legacy proposal (the exact FK omits the kind)."""
    proposal = legacy_proposal(world)
    graph = world.graph
    with tenant_context(world.tenant_id):
        return ActionExecution.objects.create(
            tenant=graph.tenant,
            client=graph.client,
            cycle=proposal.cycle,
            proposal=proposal,
            proposal_revision=proposal.revision,
            digest=proposal.digest,
            business_intent_key=proposal.business_intent_key,
            action_kind=manifestation_kind(),
            executed_by=graph.staff,
        )


def bare_client(world: StorageWorld) -> tuple[ClientCompany, ClientProviderLink]:
    """A second client of the same firm with only a provider link (no cycle graph)."""
    graph = world.graph
    source = world.capture.link
    with tenant_context(world.tenant_id):
        client = ClientCompany.objects.create(
            tenant=graph.tenant,
            legal_name="Synthetic bare client",
            cnpj="61234567000117",
            status="active",
        )
        link = ClientProviderLink.objects.create(
            tenant=graph.tenant,
            client=client,
            connection=source.connection,
            provider_company_ref=str(uuid4().int % 10**14),
            streams=["nfe_recebidas"],
            capture_start_month=source.capture_start_month,
            certificate_cnpj="61234567000117",
            certificate_valid_from=source.certificate_valid_from,
            certificate_valid_to=source.certificate_valid_to,
            credential_generation=1,
            status="ativa",
            credential_state="ativa",
        )
    return client, link


def unbound_call(
    world: StorageWorld, link: ClientProviderLink | None = None, *, label: str
) -> ProviderCall:
    """A call with no proposal or revision (0019 accepts it for any operation)."""
    fields = call_fields(world, "FC-CIE", label=label)
    if link is not None:
        fields.update(client_id=link.client_id, link=link)
    with tenant_context(world.tenant_id):
        return ProviderCall.objects.create(**fields)


def bare_exception(world: StorageWorld) -> CycleException:
    """A cycle exception without its retained ExceptionInternal companion."""
    graph = world.graph
    with tenant_context(world.tenant_id):
        return CycleException.objects.create(
            tenant=graph.tenant,
            client=graph.client,
            cycle=world.capture.setup.cycle,
            subject_ref=uuid4(),
            reason_code="manifestacao_conclusiva_pendente",
            owner=graph.staff,
            review_on=date(2026, 12, 15),
        )


def recovery_row(
    world: StorageWorld, *, call: ProviderCall, **parents: object
) -> CienciaRecord:
    """A recovery duty whose proposal/cycle/revision/engagement refs are opaque."""
    with tenant_context(world.tenant_id):
        return CienciaRecord.objects.create(
            tenant_id=world.tenant_id,
            client_id=call.client_id,
            call=call,
            environment="fake",
            document_key=key(7),
            proposal_ref=uuid4(),
            cycle_ref=uuid4(),
            fiscal_revision_ref=uuid4(),
            engagement_ref=uuid4(),
            owner=world.graph.staff,
            issued_on=date(2026, 9, 15),
            **parents,
        )
