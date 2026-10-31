"""AXRM-017: the guide reaches the client only through the existing exact approval."""

from __future__ import annotations

import io
import json
import re
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse
from django.utils import timezone

from apps.clients.models import TaxProfile
from apps.core.tenancy import tenant_context
from apps.cycles.bulk import BulkKind, BulkOperation
from apps.cycles.bulk_contracts import PreviewExpectation, Selection
from apps.cycles.bulk_runner import claim_next, execute_claim
from apps.cycles.evidence import (
    EvidenceLink,
)
from apps.cycles.models import ClientPublication
from apps.cycles.services.bulk import preview_bulk, run_bulk
from apps.integrations.budget_models import ProviderReservation
from apps.integrations.call_models import ProviderCall
from apps.obligations.models import Document
from apps.tenants.models import Tenant
from tests.fakes.serpro import FakeSerpro
from tests.integrations.pgmei_support import (
    GUIDE_PDF,
    PRICE_MICROBRL,
    GuideWorld,
    LazyModule,
    Portal,
    das_reply,
    guide_world,
    portal_member,
    publish,
    scan_and_review,
    session,
)
from tests.support import enrol_totp

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_django.fixtures import SettingsWrapper

    from apps.integrations import tasks
    from apps.integrations.serpro import pgmei
else:
    pgmei = LazyModule("apps.integrations.serpro.pgmei")
    tasks = LazyModule("apps.integrations.tasks")

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["guide_world"]


def obtain(world: GuideWorld, origin: str) -> pgmei.GuideOutcome:
    return pgmei.obtain_das_guide(
        world.owner, world.tenant_id, world.cycle.pk, origin=origin
    )


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
    assert outcome.document_id is not None
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
    assert download.content == GUIDE_PDF


def test_unpublished_guide_stays_invisible_to_the_portal(
    ready: tuple[GuideWorld, Portal],
) -> None:
    world, portal = ready
    with FakeSerpro((das_reply(),)) as peer:
        outcome = obtain(world, peer.origin)
    assert outcome.link_id is not None
    scan_and_review(world, outcome.link_id)
    assert outcome.document_id is not None
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
    settings.CLAMAV_ENABLED = True
    stdout = io.StringIO()
    from tests.integrations.pgmei_support import termo_reply  # noqa: PLC0415

    period = timezone.localdate().strftime("%Y%m")
    with FakeSerpro((termo_reply(), das_reply(period=period))) as peer:
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
