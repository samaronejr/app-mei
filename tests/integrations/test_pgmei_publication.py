"""AXRM-017: the guide reaches the client only through the existing exact approval."""

from __future__ import annotations

import io
import json
import re
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from apps.clients.models import ClientCompany, TaxProfile
from apps.core.tenancy import tenant_context
from apps.cycles.bulk import BulkKind, BulkOperation
from apps.cycles.bulk_contracts import PreviewExpectation, Selection
from apps.cycles.bulk_runner import claim_next, execute_claim
from apps.cycles.effects.executors import execute
from apps.cycles.evidence import (
    DocumentIntake,
    EvidenceLink,
    EvidenceRequest,
    IntakeState,
    RequestState,
)
from apps.cycles.models import (
    ClientPublication,
    ServiceCycle,
)
from apps.cycles.services import requests
from apps.cycles.services.approvals import approve
from apps.cycles.services.bulk import preview_bulk, run_bulk
from apps.cycles.services.publications import propose_withdrawal
from apps.integrations.budget_models import ProviderReservation
from apps.integrations.call_models import ProviderCall
from apps.obligations.models import Document
from apps.tenants.models import Tenant, TenantRole
from tests.fakes.serpro import FakeSerpro
from tests.integrations.pgmei_support import (
    GUIDE_PDF,
    PRICE_MICROBRL,
    GuideWorld,
    LazyModule,
    Portal,
    PublishedGuide,
    another_accepted_link,
    das_reply,
    guide_world,
    portal_member,
    publish,
    published_guide,
    scan_and_review,
    session,
)
from tests.portal.request_fixtures import (
    portal_setup,
    scoped_portal_cursor,
)
from tests.support import enrol_totp
from tests.ui.factories import add_member

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_django.fixtures import SettingsWrapper

    from apps.integrations import tasks
    from apps.integrations.serpro import pgmei
else:
    pgmei = LazyModule("apps.integrations.serpro.pgmei")
    tasks = LazyModule("apps.integrations.tasks")

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["guide_world", "published_guide"]
LOOKUP = "SELECT public.cycles_portal_publication_document(%s, %s)"


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
    # Start with a legacy guide request before the provider has sent anything.
    with tenant_context(world.tenant_id):
        request = EvidenceRequest.objects.create(
            tenant_id=world.tenant_id,
            client_id=world.cycle.client_id,
            cycle=world.cycle,
            title=pgmei.GUIDE_TITLE,
            state=RequestState.INTERNAL,
            required=False,
            created_by=world.owner,
        )
        public = requests.create_request(
            world.owner,
            world.cycle.pk,
            requests.RequestFields("Visible control"),
            expected_version=world.cycle.version,
        )
        world.cycle.refresh_from_db()
        requests.send_requests(
            world.owner, world.cycle.pk, expected_version=world.cycle.version
        )
    client = session(world, portal.user, portal=True)
    path = f"/solicitacoes/{request.pk}/"
    assert client.get(f"/solicitacoes/{public.pk}/").status_code == 200
    assert client.get(path).status_code == 404  # created
    assert client.post(f"/solicitacoes/{request.pk}/enviar").status_code == 404
    with FakeSerpro((das_reply(),)) as peer:
        outcome = obtain(world, peer.origin)
    assert outcome.link_id is not None
    assert outcome.document_id is not None
    link_id, document_id = outcome.link_id, outcome.document_id
    with tenant_context(world.tenant_id):
        assert DocumentIntake.objects.get(document_id=document_id).state == (
            IntakeState.QUARANTINED
        )
    assert client.get(path).status_code == 404  # private PDF ingested and linked

    def while_in_review() -> None:
        with tenant_context(world.tenant_id):
            assert DocumentIntake.objects.get(document_id=document_id).state == (
                IntakeState.CLASSIFIED
            )

    scan_and_review(world, link_id, before_review=while_in_review)
    assert client.get(path).status_code == 404  # review accepted
    proposal = publish(world, portal, link_id)
    with tenant_context(world.tenant_id):
        document = Document.objects.get(pk=outcome.document_id)
        manifest = json.loads(bytes(proposal.manifest_bytes))
        assert [row["sha256"] for row in manifest["documents"]] == [document.sha256]
        assert manifest["documents"][0]["link_id"] == str(outcome.link_id)
        publication = ClientPublication.objects.get(proposal=proposal)
        # The exact approved publication released this one private original.
        assert document.portal_visible is True
        assert document.acquisition_origin == "provedor_federal"
    assert client.get(path).status_code == 404  # publication does not release request
    page = client.get(f"/publicacoes/{publication.pk}/")
    assert page.status_code == 200
    assert document.original_filename in page.content.decode()
    download = client.get(
        f"/publicacoes/{publication.pk}/documentos/{outcome.link_id}/"
    )
    assert download.status_code == 200
    assert download.content == GUIDE_PDF


def test_withdrawn_publication_refuses_the_unchanged_guide(
    published_guide: PublishedGuide,
) -> None:
    world, publication, link = (
        published_guide.world,
        published_guide.publication,
        published_guide.link,
    )
    client = session(world, published_guide.portal.user, portal=True)
    assert client.get(published_guide.url).status_code == 200
    with tenant_context(world.tenant_id):
        withdrawal = propose_withdrawal(
            world.staff,
            publication.pk,
            expected_version=publication.version,
            reason="Retirar a guia publicada por engano.",
        )
        withdrawal = approve(world.owner, withdrawal.pk, expected_version=1).proposal
        assert execute(
            world.owner, withdrawal.pk, expected_version=withdrawal.version
        ).executed
    with scoped_portal_cursor(world.tenant_id, world.cycle.client_id) as cursor:
        cursor.execute(LOOKUP, [publication.pk, link.pk])
        assert cursor.fetchall() == [(None,)]
    assert client.get(published_guide.url).status_code == 404


def test_publication_lookup_uses_catalog_types_with_temp_schema_present(
    published_guide: PublishedGuide,
) -> None:
    world = published_guide.world
    with scoped_portal_cursor(world.tenant_id, world.cycle.client_id) as cursor:
        cursor.execute(
            """
            SELECT proconfig
            FROM pg_proc
            WHERE oid = to_regprocedure(%s)
            """,
            ["public.cycles_portal_publication_document(uuid,uuid)"],
        )
        function = cursor.fetchone()
        assert function is not None, "No scoped publication lookup on this head."
        # Harmless temporary row type of the same name as a built-in. Verify it
        # is present in the caller's session before using an approved artifact.
        cursor.execute("CREATE TEMP TABLE text (sample integer) ON COMMIT DROP")
        cursor.execute("SELECT to_regtype('text') = to_regtype('pg_temp.text')")
        assert cursor.fetchone() == (True,)
        cursor.execute(
            LOOKUP, [published_guide.publication.pk, published_guide.link.pk]
        )
        assert cursor.fetchall() == [(published_guide.link.document_id,)]
        assert function == (["search_path=pg_catalog, pg_temp"],)


def test_publication_lookup_does_not_discover_other_records(
    published_guide: PublishedGuide,
) -> None:
    world, link, publication = (
        published_guide.world,
        published_guide.link,
        published_guide.publication,
    )
    other_link = another_accepted_link(published_guide)
    assert other_link.version == link.version
    with tenant_context(world.tenant_id):
        sibling_client = ClientCompany.objects.create(
            tenant_id=world.tenant_id, cnpj="11444777000161", legal_name="Outro cliente"
        )
    sibling_user = add_member(
        world.termo.tenant,
        "another@pgmei.test",
        TenantRole.CLIENT_OWNER,
        client=sibling_client,
    )
    other_firm = portal_setup("private-guide-other-firm")
    with scoped_portal_cursor(world.tenant_id, world.cycle.client_id) as cursor:
        cursor.execute(LOOKUP, [publication.pk, link.pk])
        assert cursor.fetchall() == [(link.document_id,)]
        for publication_id, link_id in (
            (publication.pk, other_link.pk),
            (publication.pk, link.request_id),
            (link.request_id, link.pk),
            (uuid4(), link.pk),
            (publication.pk, None),
            (None, link.pk),
        ):
            cursor.execute(LOOKUP, [publication_id, link_id])
            assert cursor.fetchall() == [(None,)]
        cursor.execute(
            "SELECT pg_get_function_result('public.cycles_portal_publication_document"
            "(uuid,uuid)'::regprocedure)"
        )
        assert cursor.fetchone() == ("uuid",)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT pg_get_userbyid(acl.grantee)
            FROM pg_proc proc,
                aclexplode(coalesce(proc.proacl, acldefault('f', proc.proowner))) acl
            WHERE proc.oid = 'public.cycles_portal_publication_document(uuid,uuid)'
                ::regprocedure
              AND acl.grantee <> proc.proowner AND acl.privilege_type = 'EXECUTE'
            """
        )
        assert cursor.fetchall() == [("app_portal",)]
    for tenant_id, client_id in (
        (world.tenant_id, sibling_client.pk),
        (other_firm.graph.tenant.pk, world.cycle.client_id),
        (other_firm.graph.tenant.pk, other_firm.graph.client.pk),
    ):
        with scoped_portal_cursor(tenant_id, client_id) as cursor:
            cursor.execute(LOOKUP, [publication.pk, link.pk])
            assert cursor.fetchall() == [(None,)]
    with scoped_portal_cursor(world.tenant_id, world.cycle.client_id) as cursor:
        for dimension, original in (
            ("app.client_id", world.cycle.client_id),
            ("app.tenant_id", world.tenant_id),
        ):
            cursor.execute("SELECT set_config(%s, '', true)", [dimension])
            cursor.execute(LOOKUP, [publication.pk, link.pk])
            assert cursor.fetchall() == [(None,)]
            cursor.execute(
                "SELECT set_config(%s, %s, true)", [dimension, str(original)]
            )
    url = published_guide.url
    assert session(world, sibling_user, portal=True).get(url).status_code == 404
    assert other_firm.portal().get(url).status_code == 404


def test_publication_lookup_refuses_stale_document_or_link(
    published_guide: PublishedGuide,
) -> None:
    world, link, publication = (
        published_guide.world,
        published_guide.link,
        published_guide.publication,
    )
    client = session(world, published_guide.portal.user, portal=True)
    url = published_guide.url
    assert client.get(url).status_code == 200
    with tenant_context(world.tenant_id):
        document = Document.objects.get(pk=link.document_id)
    for field, changed in (
        ("sha256", "b" * 64),
        ("byte_size", document.byte_size + 1),
        ("portal_visible", False),
    ):
        with tenant_context(world.tenant_id):
            Document.objects.filter(pk=document.pk).update(**{field: changed})
        with scoped_portal_cursor(world.tenant_id, world.cycle.client_id) as cursor:
            cursor.execute(LOOKUP, [publication.pk, link.pk])
            assert cursor.fetchall() == [(None,)], field
        assert client.get(url).status_code == 404
        with tenant_context(world.tenant_id):
            Document.objects.filter(pk=document.pk).update(
                **{field: getattr(document, field)}
            )
    with tenant_context(world.tenant_id):
        EvidenceLink.objects.filter(pk=link.pk).update(client_reason="Nova revisão")
    with scoped_portal_cursor(world.tenant_id, world.cycle.client_id) as cursor:
        cursor.execute(LOOKUP, [publication.pk, link.pk])
        assert cursor.fetchall() == [(None,)]
    assert client.get(url).status_code == 404


def test_live_request_must_still_belong_to_the_publication_cycle(
    published_guide: PublishedGuide,
) -> None:
    world, link, publication = (
        published_guide.world,
        published_guide.link,
        published_guide.publication,
    )
    with tenant_context(world.tenant_id):
        original = world.cycle
        another = ServiceCycle.objects.create(
            tenant_id=original.tenant_id,
            client_id=original.client_id,
            engagement_id=original.engagement_id,
            playbook_id=original.playbook_id,
            playbook_version_id=original.playbook_version_id,
            period_start=original.period_start.replace(
                year=original.period_start.year - 1
            ),
            period_end=original.period_end.replace(year=original.period_end.year - 1),
        )
        EvidenceRequest.objects.filter(pk=link.request_id).update(cycle=another)
    with scoped_portal_cursor(world.tenant_id, world.cycle.client_id) as cursor:
        cursor.execute(LOOKUP, [publication.pk, link.pk])
        assert cursor.fetchall() == [(None,)]


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
    unpublished = uuid4()
    with scoped_portal_cursor(world.tenant_id, world.cycle.client_id) as cursor:
        cursor.execute(LOOKUP, [unpublished, outcome.link_id])
        assert cursor.fetchall() == [(None,)]
    path = f"/publicacoes/{unpublished}/documentos/{outcome.link_id}/"
    assert client.get(path).status_code == 404


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
