"""Live selected sources, not synthetic manifest-only changes."""

import base64
import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, timedelta
from email.parser import BytesParser
from email.policy import default
from email.utils import parsedate_to_datetime
from pathlib import Path

import pytest
from django.db import transaction
from django.db.models.signals import post_save
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.clients.models import ClientAssignment
from apps.core.tenancy import tenant_context
from apps.cycles.effects.build import BuildError
from apps.cycles.effects.inputs import MoneyBinding, ProposalDraft, ProposalInput
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.models import (
    ActionProposal,
    EvidenceLink,
    ExtractedField,
    ProposalState,
)
from apps.cycles.services.approvals import approve, propose, revalidate
from apps.cycles.services.review import record_review
from apps.obligations.generator import generate_das_calendar
from apps.obligations.models import Obligation, SourceObservation
from tests.cycles.approval_fixtures import ApprovalGraph, approval_graph
from tests.cycles.document_fixtures import reviewed
from tests.portal.request_fixtures import portal_setup

pytestmark = pytest.mark.django_db(transaction=True)


def test_email_freezes_reproducible_transport_bytes_and_revalidates_sender(
    monkeypatch: pytest.MonkeyPatch, settings: SettingsWrapper
) -> None:
    setup = approval_graph("approval-email-transport")
    with tenant_context(setup.graph.tenant.pk):
        frozen = bytes(setup.proposal.manifest_bytes)
        payload = json.loads(frozen)["payload"]
        wire = base64.b64decode(payload["mime_base64"], validate=True)
        message = BytesParser(policy=default).parsebytes(wire)
        assert hashlib.sha256(wire).hexdigest() == payload["mime_sha256"]
        assert str(message["From"]) == settings.DEFAULT_FROM_EMAIL
        assert str(message["To"]) == setup.contact.email
        assert str(message["Subject"]) == "Documentos de setembro"
        assert message.get_content().rstrip("\r\n") == (
            "Confira os documentos selecionados."
        )
        assert payload["headers"]["Message-ID"] == (
            f"<{setup.proposal.pk}@outbox.local>"
        )
        assert parsedate_to_datetime(payload["headers"]["Date"]) == (
            setup.proposal.created_at.astimezone(UTC).replace(microsecond=0)
        )
        future = timezone.now() + timedelta(minutes=1)
        monkeypatch.setattr(timezone, "now", lambda: future)
        assert approve(
            setup.graph.actor, setup.proposal.pk, expected_version=1
        ).executable
        assert revalidate(setup.graph.actor, setup.proposal.pk).executable
        assert bytes(setup.proposal.manifest_bytes) == frozen
        settings.DEFAULT_FROM_EMAIL = "another-sender@example.test"
        result = revalidate(setup.graph.actor, setup.proposal.pk)
        assert not result.executable
        assert result.proposal.state == ProposalState.INVALIDATED
        assert bytes(result.proposal.manifest_bytes) == frozen


def _source(setup: ApprovalGraph, *, official: bool = False) -> SourceObservation:
    generate_das_calendar(setup.graph.client, starting_from=setup.cycle.period_start)
    subject = Obligation.objects.get(
        client=setup.graph.client, competence_month=setup.cycle.period_start
    )
    with transaction.atomic():
        subject = Obligation.objects.select_for_update(no_key=True).get(pk=subject.pk)
        return SourceObservation.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            subject_ref=subject,
            period=setup.cycle.period_start,
            kind="payment",
            source_kind="official_api" if official else "staff_recorded",
            verification_status="verified" if official else "unknown",
            observed_at=timezone.now(),
        )


def _select(setup: ApprovalGraph, source: SourceObservation) -> ActionProposal:
    return propose(
        setup.graph.staff,
        setup.cycle.pk,
        ProposalDraft(
            replace(
                ProposalInput.from_data(setup.proposal.input_spec),
                observation_ids=(source.pk,),
            ),
            setup.proposal.expires_at,
            previous_id=setup.proposal.pk,
            previous_version=1,
        ),
        expected_version=setup.cycle.version,
    )


@pytest.mark.parametrize(
    "change", ["backdated", "forward", "subject", "kind", "period", "failed"]
)
def test_only_exact_scoped_successors_invalidate_the_selected_assertion(
    change: str,
) -> None:
    setup = approval_graph("approval-source")
    with tenant_context(setup.graph.tenant.pk):
        source = _source(setup)
        proposal = _select(setup, source)
        approve(setup.graph.actor, proposal.pk, expected_version=1)
        frozen = bytes(proposal.manifest_bytes)
        subject_id = source.subject_ref_id
        if change == "subject":
            subject_id = Obligation.objects.get(
                client=setup.graph.client, competence_month=date(2026, 10, 1)
            ).pk
        with transaction.atomic():
            Obligation.objects.select_for_update(no_key=True).get(pk=subject_id)
            SourceObservation.objects.create(
                tenant=setup.graph.tenant,
                client=setup.graph.client,
                subject_ref_id=subject_id,
                period=date(2026, 10, 1) if change == "period" else source.period,
                kind="guide_issued" if change == "kind" else source.kind,
                source_kind="staff_recorded",
                verification_status="refresh_failed"
                if change == "failed"
                else "unknown",
                observed_at=source.observed_at
                + (-timedelta(days=1) if change == "backdated" else timedelta(days=1)),
                supersedes=source if change != "failed" else None,
            )
        result = revalidate(setup.graph.actor, proposal.pk)
        assert result.executable is (change not in {"backdated", "forward"})
        assert bytes(result.proposal.manifest_bytes) == frozen
        assert [row["id"] for row in json.loads(frozen)["observations"]] == [
            str(source.pk)
        ]
        source.refresh_from_db()
        assert source.source_kind == "staff_recorded"


def test_expired_official_observation_warns_for_email_without_changing_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = approval_graph("approval-freshness")
    with tenant_context(setup.graph.tenant.pk):
        source = _source(setup, official=True)
        # The proposal must outlive the observation to isolate source freshness.
        proposal = propose(
            setup.graph.staff,
            setup.cycle.pk,
            ProposalDraft(
                replace(
                    ProposalInput.from_data(setup.proposal.input_spec),
                    observation_ids=(source.pk,),
                ),
                source.observed_at + timedelta(days=8),
                previous_id=setup.proposal.pk,
                previous_version=1,
            ),
            expected_version=setup.cycle.version,
        )
        approve(setup.graph.actor, proposal.pk, expected_version=1)
        frozen = bytes(proposal.manifest_bytes)
        monkeypatch.setattr(
            timezone, "now", lambda: source.observed_at + timedelta(days=7)
        )
        result = revalidate(setup.graph.actor, proposal.pk)
        assert result.executable
        assert [(item.code, item.impact.value) for item in result.reasons] == [
            ("source_stale", "warn")
        ]
        assert bytes(result.proposal.manifest_bytes) == frozen


def test_portal_review_projection_alone_cannot_authorize_a_document(
    settings: SettingsWrapper,
    tmp_path: Path,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    setup = portal_setup("approval-forged-review")
    _intake, link = reviewed(setup, "nfe_emitente_cliente.xml")
    with tenant_context(setup.graph.tenant.pk):
        EvidenceLink.objects.filter(pk=link.pk).update(review_decision="aceito")
        setup.cycle.refresh_from_db()
        draft = ProposalDraft(
            ProposalInput(
                action_kind=ActionKind.EMAIL_NOTICE,
                step_key="attachment",
                recipient_ids=(setup.contact.pk,),
                document_link_ids=(link.pk,),
                subject="Nota selecionada",
                body="Confira a nota anexada.",
            ),
            timezone.now() + timedelta(days=1),
        )
        with pytest.raises(BuildError, match="document_not_reviewed"):
            propose(
                setup.graph.actor,
                setup.cycle.pk,
                draft,
                expected_version=setup.cycle.version,
            )
        assert not ActionProposal.objects.exists()


def test_actual_review_link_revision_and_extracted_amount_are_bound(
    settings: SettingsWrapper,
    tmp_path: Path,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    setup = portal_setup("approval-reviewed-source")
    _intake, link = reviewed(setup, "nfe_emitente_cliente.xml")
    with tenant_context(setup.graph.tenant.pk):
        ClientAssignment.objects.create(
            tenant=setup.graph.tenant, client=setup.graph.client, user=setup.graph.staff
        )
        setup.request.refresh_from_db()
        observed_versions: list[int] = []

        def observe_link(*, instance: EvidenceLink, **_kwargs: object) -> None:
            observed_versions.append(instance.version)

        post_save.connect(observe_link, sender=EvidenceLink, weak=False)
        try:
            record_review(
                setup.graph.actor,
                link.pk,
                decision="aceito",
                client_reason="",
                corrections=[],
                expected_version=setup.request.version,
            )
        finally:
            post_save.disconnect(observe_link, sender=EvidenceLink)
        link.refresh_from_db()
        assert link.version > 1
        assert observed_versions == [link.version]
        setup.cycle.refresh_from_db()
        proposal = propose(
            setup.graph.staff,
            setup.cycle.pk,
            ProposalDraft(
                ProposalInput(
                    action_kind=ActionKind.EMAIL_NOTICE,
                    step_key="attachment",
                    recipient_ids=(setup.contact.pk,),
                    document_link_ids=(link.pk,),
                    money=(MoneyBinding(link.pk, "valor_total"),),
                    subject="Nota selecionada",
                    body="Confira a nota anexada.",
                ),
                timezone.now() + timedelta(days=1),
            ),
            expected_version=setup.cycle.version,
        )
        frozen = json.loads(bytes(proposal.manifest_bytes))
        assert frozen["documents"][0]["link_revision"] == link.version
        assert frozen["documents"][0]["review_state"] == "aceito"
        approve(setup.graph.actor, proposal.pk, expected_version=1)
        # A trusted field correction is observed live, not copied from input_spec.
        # The same cycle fence is held by the actual review/extraction writers.
        with transaction.atomic():
            type(setup.cycle).objects.select_for_update().get(pk=setup.cycle.pk)
            ExtractedField.objects.create(
                tenant=setup.graph.tenant,
                client=setup.graph.client,
                document_id=link.document_id,
                name="valor_total",
                value="9999.99",
                origin="staff",
                recorded_by=setup.graph.actor,
            )
        result = revalidate(setup.graph.actor, proposal.pk)
        assert result.proposal.state == ProposalState.INVALIDATED
        assert "amount_changed" in {item.code for item in result.reasons}
        assert json.loads(bytes(result.proposal.manifest_bytes)) == frozen
