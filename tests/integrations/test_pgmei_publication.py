"""AXRM-017: the guide reaches the client only through the existing exact approval."""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.clients.models import ClientContact, TaxProfile
from apps.core.tenancy import tenant_context
from apps.cycles.bulk import BulkKind, BulkOperation
from apps.cycles.bulk_contracts import PreviewExpectation, Selection
from apps.cycles.bulk_runner import claim_next, execute_claim
from apps.cycles.effects.executors import execute
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.evidence import (
    DocumentIntake,
    EvidenceLink,
    EvidenceRequest,
    IntakeState,
    ReviewDecision,
)
from apps.cycles.extraction.service import extract_intake
from apps.cycles.models import ActionProposal, ClientPublication
from apps.cycles.scanning import FakeScanner
from apps.cycles.services.approvals import approve, propose
from apps.cycles.services.bulk import preview_bulk, run_bulk
from apps.cycles.services.review import record_review
from apps.cycles.tasks import scan_quarantine
from apps.integrations.budget_models import ProviderReservation
from apps.integrations.call_models import ProviderCall
from apps.obligations.models import Document
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.browser.harness import mfa_session_cookie
from tests.fakes.serpro import FakeSerpro
from tests.integrations.pgmei_support import (
    GUIDE_PDF,
    PRICE_MICROBRL,
    GuideWorld,
    LazyModule,
    das_reply,
    guide_world,
)
from tests.support import enrol_totp
from tests.ui.factories import add_member

if TYPE_CHECKING:
    from pathlib import Path
    from uuid import UUID

    from pytest_django.fixtures import SettingsWrapper

    from apps.accounts.models import User
    from apps.integrations.serpro import pgmei
else:
    pgmei = LazyModule("apps.integrations.serpro.pgmei")

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["guide_world"]


@dataclass(frozen=True)
class Portal:
    user: User
    contact: ClientContact


def portal_member(world: GuideWorld) -> Portal:
    tenant = world.termo.tenant
    user = add_member(
        tenant, "cliente@pgmei.test", TenantRole.CLIENT_OWNER, client=world.termo.client
    )
    with tenant_context(tenant.pk):
        # PLATFORM_QUERY_OK: test setup reads the seat it just created.
        member = Membership.objects.get(tenant=tenant, user=user)
        contact = ClientContact.objects.create(
            tenant=tenant,
            client=world.termo.client,
            membership=member,
            name="Pessoa MEI",
            email=user.email,
            channel_email_allowed=True,
        )
    return Portal(user, contact)


def session(world: GuideWorld, user: User, *, portal: bool) -> Client:
    host = f"{world.termo.tenant.slug}{'-portal' if portal else ''}.localhost"
    client = Client(SERVER_NAME=host)
    cookie = mfa_session_cookie(user, f"http://{host}/")
    client.cookies[cookie["name"]] = cookie["value"]  # type: ignore[typeddict-item]
    return client


def obtain(world: GuideWorld, origin: str) -> pgmei.GuideOutcome:
    return pgmei.obtain_das_guide(
        world.owner, world.tenant_id, world.cycle.pk, origin=origin
    )


def scan_and_review(world: GuideWorld, link_id: UUID) -> None:
    with patch("apps.cycles.tasks.scanner_from_settings", return_value=FakeScanner()):
        assert scan_quarantine.apply().get() >= 1
    with tenant_context(world.tenant_id):
        link = EvidenceLink.objects.get(pk=link_id)
        intake = DocumentIntake.objects.get(document_id=link.document_id)
    extract_intake(world.tenant_id, intake.pk, now=timezone.now())
    with tenant_context(world.tenant_id):
        intake.refresh_from_db()
        assert intake.state != IntakeState.QUARANTINED
        request = EvidenceRequest.objects.get(pk=link.request_id)
        record_review(
            world.staff,
            link.pk,
            decision=ReviewDecision.ACCEPTED,
            client_reason="",
            corrections=[],
            expected_version=request.version,
        )
        request.refresh_from_db()
        # Accepting the guide never turns the firm-only request client-visible.
        assert request.state == pgmei.FIRM_ONLY_STATE


def publish(
    world: GuideWorld,
    portal: Portal,
    link_id: UUID,
    *,
    occurrence: int = 1,
    supersedes: UUID | None = None,
) -> ActionProposal:
    with tenant_context(world.tenant_id):
        world.cycle.refresh_from_db()
        proposal = propose(
            world.staff,
            world.cycle.pk,
            ProposalDraft(
                ProposalInput(
                    action_kind=ActionKind.CLIENT_PUBLICATION,
                    step_key="publicar-guia-das",
                    occurrence=occurrence,
                    recipient_ids=(portal.contact.pk,),
                    document_link_ids=(link_id,),
                    subject="Guia DAS de outubro",
                    body="A guia DAS do mês está disponível.",
                    supersedes_publication_id=supersedes,
                ),
                timezone.now() + timedelta(days=1),
            ),
            expected_version=world.cycle.version,
        )
        approved = approve(world.owner, proposal.pk, expected_version=1).proposal
        assert execute(
            world.owner, approved.pk, expected_version=approved.version
        ).executed
        return ActionProposal.objects.get(pk=proposal.pk)


@pytest.fixture
def ready(guide_world: GuideWorld) -> tuple[GuideWorld, Portal]:
    enrol_totp(guide_world.owner)
    enrol_totp(guide_world.staff)
    return guide_world, portal_member(guide_world)


def test_approval_digest_binds_the_guide_hash_and_the_portal_receives_it(
    ready: tuple[GuideWorld, Portal],
) -> None:
    world, portal = ready
    with FakeSerpro((das_reply(),)) as peer:
        outcome = obtain(world, peer.origin)
    assert outcome.link_id is not None
    scan_and_review(world, outcome.link_id)
    proposal = publish(world, portal, outcome.link_id)
    with tenant_context(world.tenant_id):
        document = Document.objects.get(pk=outcome.document_id)
        manifest = json.loads(bytes(proposal.manifest_bytes))
        assert [row["sha256"] for row in manifest["documents"]] == [document.sha256]
        assert manifest["documents"][0]["link_id"] == str(outcome.link_id)
        publication = ClientPublication.objects.get(proposal=proposal)
        # The exact approved publication released this one private original.
        assert document.portal_visible is True
        assert document.acquisition_origin == "provedor_federal"
    client = session(world, portal.user, portal=True)
    page = client.get(f"/publicacoes/{publication.pk}/")
    assert page.status_code == 200
    assert document.original_filename in page.content.decode()
    download = client.get(
        f"/publicacoes/{publication.pk}/documentos/{outcome.link_id}/"
    )
    assert download.status_code == 200
    assert b"".join(download.streaming_content) == GUIDE_PDF  # type: ignore[attr-defined]


def test_unpublished_guide_stays_invisible_to_the_portal(
    ready: tuple[GuideWorld, Portal],
) -> None:
    world, portal = ready
    with FakeSerpro((das_reply(),)) as peer:
        outcome = obtain(world, peer.origin)
    assert outcome.link_id is not None
    scan_and_review(world, outcome.link_id)
    with tenant_context(world.tenant_id):
        assert Document.objects.get(pk=outcome.document_id).portal_visible is False
        request_id = EvidenceLink.objects.get(pk=outcome.link_id).request_id
    client = session(world, portal.user, portal=True)
    home = client.get("/")
    assert home.status_code == 200
    assert pgmei.GUIDE_TITLE not in home.content.decode()
    assert client.get(f"/solicitacoes/{request_id}/").status_code == 404


def test_regenerated_guide_is_published_as_a_second_occurrence(
    ready: tuple[GuideWorld, Portal], monkeypatch: pytest.MonkeyPatch
) -> None:
    # R1 T69: the occurrence field admits a second publication of the same step.
    world, portal = ready
    with FakeSerpro((das_reply(), das_reply(pdf=GUIDE_PDF + b"% reissued\n"))) as peer:
        first = obtain(world, peer.origin)
        assert first.link_id is not None
        scan_and_review(world, first.link_id)
        published = publish(world, portal, first.link_id)
        tomorrow = timezone.localdate() + timedelta(days=1)
        monkeypatch.setattr(pgmei, "consolidation_day", lambda: tomorrow)
        second = obtain(world, peer.origin)
    assert second.link_id is not None
    scan_and_review(world, second.link_id)
    with tenant_context(world.tenant_id):
        previous = ClientPublication.objects.get(proposal=published)
    republished = publish(
        world, portal, second.link_id, occurrence=2, supersedes=previous.pk
    )
    with tenant_context(world.tenant_id):
        current = ClientPublication.objects.get(proposal=republished)
        assert current.supersedes_id == previous.pk
        assert published.business_intent_key != republished.business_intent_key


def test_cycle_page_offers_the_action_only_to_eligible_staff(
    guide_world: GuideWorld,
) -> None:
    enrol_totp(guide_world.owner)
    enrol_totp(guide_world.ops)
    url = reverse("cycle-detail", args=[guide_world.cycle.pk])
    owner_page = session(guide_world, guide_world.owner, portal=False).get(url)
    assert owner_page.status_code == 200
    assert "Obter guia DAS oficial" in owner_page.content.decode()
    with tenant_context(guide_world.tenant_id):
        TaxProfile.objects.filter(client=guide_world.termo.client).update(
            regime="simples"
        )
    hidden = session(guide_world, guide_world.owner, portal=False).get(url)
    assert "Obter guia DAS oficial" not in hidden.content.decode()


def test_post_enqueues_the_guide_after_commit_and_operations_is_refused(
    guide_world: GuideWorld,
    django_capture_on_commit_callbacks: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks = LazyModule("apps.integrations.tasks")
    enrol_totp(guide_world.owner)
    enrol_totp(guide_world.ops)
    queued: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        tasks.obtain_das_guide_task, "delay", lambda *args: queued.append(args)
    )
    url = reverse("cycle-das-guide", args=[guide_world.cycle.pk])
    refused = session(guide_world, guide_world.ops, portal=False).post(url)
    assert refused.status_code in {403, 404}
    with django_capture_on_commit_callbacks(execute=True):  # type: ignore[operator]
        response = session(guide_world, guide_world.owner, portal=False).post(url)
    assert response.status_code == 302
    assert queued == [
        (
            str(guide_world.tenant_id),
            str(guide_world.owner.pk),
            str(guide_world.cycle.pk),
        )
    ]
    with FakeSerpro((das_reply(),)) as peer, pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(pgmei, "fake_origin", lambda: peer.origin)
        code = tasks.obtain_das_guide_task(*queued[0])
        assert code == "guia_obtida"
        assert len(peer.requests) == 1


def test_bulk_preview_shows_the_per_item_cost_and_reserves_per_item(
    guide_world: GuideWorld,
) -> None:
    enrol_totp(guide_world.owner)
    with tenant_context(guide_world.tenant_id):
        operation = preview_bulk(
            guide_world.owner,
            BulkKind.OBTAIN_DAS_GUIDES,
            Selection((guide_world.cycle.pk,)),
        )
        [item] = operation.snapshot_json["items"]
        assert item["eligible"] is True
        assert item["effect"]["estimated_cost_microbrl"] == PRICE_MICROBRL
        assert operation.snapshot_json["estimated_cost"] == PRICE_MICROBRL
        assert not ProviderCall.objects.exists()
    page = session(guide_world, guide_world.owner, portal=False).get(
        reverse("bulk-detail", args=[operation.pk])
    )
    assert page.status_code == 200
    text = page.content.decode()
    assert re.search(r"R\$\s*0,32", text)
    assert "data-bulk-cost" in text
    with tenant_context(guide_world.tenant_id):
        queued = run_bulk(
            guide_world.owner,
            operation.pk,
            confirm_count=1,
            exclusions=(),
            expectation=PreviewExpectation(
                operation.version, operation.snapshot_digest
            ),
        )
    with FakeSerpro((das_reply(),)) as peer, pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(pgmei, "fake_origin", lambda: peer.origin)
        claim = claim_next(guide_world.tenant_id, queued.pk)
        assert claim is not None
        execute_claim(claim)
        assert len(peer.requests) == 1
    with tenant_context(guide_world.tenant_id):
        call = ProviderCall.objects.get(operation="SP-DAS")
        assert ProviderReservation.objects.filter(call=call).count() == 1
        row = BulkOperation.objects.get(pk=queued.pk).items.get()
        assert row.state == "done"
        assert row.result_json["code"] == "guia_obtida"
        assert EvidenceLink.objects.filter(request__cycle=guide_world.cycle).exists()


def test_bulk_preview_marks_a_non_mei_cycle_ineligible(
    guide_world: GuideWorld,
) -> None:
    with tenant_context(guide_world.tenant_id):
        TaxProfile.objects.filter(client=guide_world.termo.client).update(
            regime="simples"
        )
        operation = preview_bulk(
            guide_world.owner,
            BulkKind.OBTAIN_DAS_GUIDES,
            Selection((guide_world.cycle.pk,)),
        )
        [item] = operation.snapshot_json["items"]
        assert (item["eligible"], item["reason"]) == (False, "nao_mei")
        assert operation.snapshot_json["estimated_cost"] == 0


def test_fake_pgmei_qualification_writes_secret_free_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: SettingsWrapper
) -> None:
    monkeypatch.chdir(tmp_path)
    settings.MEDIA_ROOT = tmp_path / "media"
    stdout = io.StringIO()
    from tests.integrations.pgmei_support import termo_reply  # noqa: PLC0415

    with FakeSerpro((termo_reply(), das_reply())) as peer:
        settings.SERPRO_FAKE_ORIGIN = peer.origin
        call_command(
            "qualify_provider",
            "serpro",
            "--scenario",
            "pgmei",
            "--environment",
            "fake",
            stdout=stdout,
        )
        assert [r.path for r in peer.requests] == ["/Apoiar", "/Emitir"]
    [path] = list((tmp_path / ".evidence").glob("AXRM-PQ-serpro-pgmei-*.txt"))
    raw = path.read_text()
    evidence = json.loads(raw)
    assert (evidence["scenario"], evidence["environment"]) == ("pgmei", "fake")
    assert evidence["http_status"] == 200
    assert evidence["billing_class"] == "emissao"
    assert evidence["parsed_fields"]["guide_stored"] is True
    assert evidence["parsed_fields"]["intake_state"] == "quarentena"
    for canary in ("12ABC34501DE35", "JVBERi0x", "synthetic-procurador"):
        assert canary not in raw + stdout.getvalue()
    assert not Tenant.objects.filter(is_active=True).exists()


def test_pgmei_qualification_refuses_production_before_any_request(
    settings: SettingsWrapper,
) -> None:
    with FakeSerpro((das_reply(),)) as peer:
        settings.SERPRO_FAKE_ORIGIN = peer.origin
        with pytest.raises(CommandError):
            call_command(
                "qualify_provider",
                "serpro",
                "--scenario",
                "pgmei",
                "--environment",
                "producao",
            )
        assert peer.requests == []
    assert not Tenant.objects.exists()
