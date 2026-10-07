"""Retention protects every supported content stream and keeps immutable proof."""

import hashlib
import logging
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Self

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.authz.models import Role
from apps.core.retention import RetentionClass
from apps.core.tenancy import tenant_context
from apps.cycles.completion_checks import completion_items
from apps.cycles.completion_records import CompletionGeneration, CompletionJustification
from apps.cycles.content_redaction import REDACTED
from apps.cycles.effects.executors import execute
from apps.cycles.evidence_index import capture_index
from apps.cycles.models import (
    ActionProposal,
    ClientPublication,
    CycleAmendment,
    CycleState,
    ExceptionInternal,
    ExceptionState,
    ExtractedField,
    InternalNote,
    RequestInternal,
    ServiceCycle,
)
from apps.cycles.openers import REQUEST_OVERDUE
from apps.cycles.retention import place_hold, release_hold
from apps.cycles.retention_records import ContentPurgeClaim
from apps.cycles.retention_tasks import purge_expired_content, purge_tenant
from apps.cycles.services.approvals import approve
from apps.cycles.services.completion import propose_reopen
from apps.cycles.services.exceptions import open_exception, transition_exception
from apps.cycles.services.requests import RequestFields, create_request
from apps.delivery.models import OutboundMessage
from apps.obligations.models import Document
from apps.tenants.models import Membership
from tests.cycles.completion_fixtures import CompletionSetup, approved_completion
from tests.cycles.publication_fixtures import PublicationGraph, publication_graph
from tests.cycles.test_cycle_completion import ready_cycle

pytestmark = pytest.mark.django_db(transaction=True)
PAST = datetime(2020, 1, 15, 12, tzinfo=UTC)
NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)
CANARY = "PRIVATE-RETENTION-CONTENT-CANARY"


class HistoricalClock(datetime):
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> Self:
        return cls.fromtimestamp(PAST.timestamp(), tz=tz)


def private_content(pub: PublicationGraph, cycle: ServiceCycle) -> Document:
    request = create_request(
        pub.graph.actor,
        cycle.pk,
        RequestFields(
            "Documento opcional",
            required=False,
            instructions=CANARY,
            due_on=PAST.date(),
        ),
        expected_version=cycle.version,
    )
    cycle.refresh_from_db()
    problem = open_exception(
        REQUEST_OVERDUE,
        request.pk,
        actor=pub.graph.actor,
        cycle_id=cycle.pk,
        expected_version=cycle.version,
        owner=pub.graph.staff,
        review_on=PAST.date(),
    )
    problem = transition_exception(
        pub.graph.actor,
        problem.pk,
        ExceptionState.HANDLING,
        expected_version=problem.version,
    )
    transition_exception(
        pub.graph.actor,
        problem.pk,
        ExceptionState.RESOLVED,
        expected_version=problem.version,
        note=CANARY,
    )
    InternalNote.objects.create(
        tenant=pub.graph.tenant,
        client=pub.graph.client,
        cycle=cycle,
        author=pub.graph.actor,
        body=CANARY,
    )
    contents = b"unexpired unknown-period source"
    key = default_storage.save("retained/unknown-period.bin", ContentFile(contents))
    document = Document.objects.create(
        tenant=pub.graph.tenant,
        client=pub.graph.client,
        storage_key=key,
        sha256=hashlib.sha256(contents).hexdigest(),
        original_filename="source.bin",
        content_type="application/octet-stream",
        byte_size=len(contents),
        uploaded_by=pub.graph.actor,
    )
    ExtractedField.objects.create(
        tenant=pub.graph.tenant,
        client=pub.graph.client,
        document=document,
        name="reference",
        value=CANARY,
        source_span=CANARY,
        origin="staff",
        retention_class=RetentionClass.AI_DERIVED,
    )
    return document


def historical_content(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[PublicationGraph, Document]:
    with monkeypatch.context() as clock:
        # DateTimeField defaults retain the timezone.now callable. Override the
        # clock it reads so both the service and captured defaults see one time.
        clock.setattr(timezone, "datetime", HistoricalClock)
        pub = publication_graph("retention-streams")
        with tenant_context(pub.graph.tenant.pk):
            assert execute(
                pub.graph.actor,
                pub.proposal.pk,
                expected_version=pub.proposal.version,
            ).executed
            cycle = ready_cycle(pub.graph)
            document = private_content(pub, cycle)
            cycle.refresh_from_db()
            capture_index(pub.graph.actor, cycle.pk, expected_version=cycle.version)
            proposal = approved_completion(
                CompletionSetup(pub.graph, cycle),
                state=CycleState.WITH_PENDING,
                reasons={item.key: CANARY for item in completion_items(cycle)},
                capture=False,
            )
            assert execute(
                pub.graph.actor, proposal.pk, expected_version=proposal.version
            ).executed
            reviewer = User.objects.create_user(email="reviewer@retention.test")
            Membership.objects.create(
                tenant=pub.graph.tenant, user=reviewer, role=Role.OPERATIONS_ADMIN
            )
            cycle.refresh_from_db()
            reopening = propose_reopen(
                pub.graph.actor,
                cycle.pk,
                reason=CANARY,
                affected_scope=(cycle.pk,),
                expected_version=cycle.version,
            )
            reopening = approve(
                reviewer, reopening.pk, expected_version=reopening.version
            ).proposal
            assert execute(
                reviewer, reopening.pk, expected_version=reopening.version
            ).executed
            assert proposal.created_at == PAST
    return pub, document


def test_dry_run_and_hold_cover_frozen_bodies_notes_and_ai_content(
    settings: SettingsWrapper,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    pub, document = historical_content(monkeypatch)
    with tenant_context(pub.graph.tenant.pk):
        generation = CompletionGeneration.objects.get(cycle=pub.cycle)
        snapshot = generation.snapshot
        proof = dict(ActionProposal.objects.values_list("pk", "digest"))
    with caplog.at_level(logging.INFO, logger="apps.cycles.retention_tasks"):
        counts = purge_expired_content(now=NOW)
    assert counts == {
        "eligible": 12,
        "held": 0,
        "retained": 1,
        "purged": 0,
        "failed": 0,
    }
    log = next(
        record
        for record in caplog.records
        if record.name == "apps.cycles.retention_tasks"
    )
    assert isinstance(log.args, tuple)
    assert all(isinstance(value, int) for value in log.args)
    assert CANARY not in log.getMessage()
    with tenant_context(pub.graph.tenant.pk):
        assert not ContentPurgeClaim.objects.exists()
        assert ExtractedField.objects.get(document=document).value == CANARY
        hold = place_hold(
            pub.graph.actor,
            pub.graph.client.pk,
            reason="Retenção profissional",
            expected_version=pub.graph.client.version,
        )
    held = purge_tenant(pub.graph.tenant.pk, dry_run=False, now=NOW)
    assert (held["held"], held["purged"]) == (13, 0)
    with tenant_context(pub.graph.tenant.pk):
        assert not ContentPurgeClaim.objects.exists()
        release_hold(pub.graph.actor, hold.pk, expected_version=hold.version)
    purged = purge_tenant(pub.graph.tenant.pk, dry_run=False, now=NOW)
    assert (purged["purged"], purged["failed"], purged["retained"]) == (12, 0, 1)
    with tenant_context(pub.graph.tenant.pk):
        assert set(
            ContentPurgeClaim.objects.values_list("content_kind", flat=True)
        ) == {
            "proposal",
            "publication",
            "message",
            "note",
            "justification",
            "request_internal",
            "exception_internal",
            "amendment",
            "ai_derived",
        }
        assert dict(ActionProposal.objects.values_list("pk", "digest")) == proof
        assert all(
            CANARY.encode() not in bytes(proposal.manifest_bytes)
            for proposal in ActionProposal.objects.all()
        )
        assert (
            ClientPublication.objects.get(proposal=pub.proposal).body_ptbr == REDACTED
        )
        assert OutboundMessage.objects.get(proposal_id=pub.proposal.pk).subject == ""
        assert not InternalNote.objects.exclude(body="").exists()
        assert RequestInternal.objects.get(request__cycle=pub.cycle).instructions == ""
        assert (
            ExceptionInternal.objects.get(exception__cycle=pub.cycle).next_action == ""
        )
        assert CycleAmendment.objects.get(cycle=pub.cycle).reason == ""
        assert CompletionJustification.objects.get(generation=generation).reasons == {}
        assert ExtractedField.objects.get(document=document).value == ""
        generation.refresh_from_db()
        assert generation.snapshot == snapshot
        document.refresh_from_db()
        assert document.purged_at is None
    assert default_storage.exists(document.storage_key)
