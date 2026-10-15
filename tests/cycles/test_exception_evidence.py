"""Batched evidence resolution refuses untrusted references and selected identities."""

from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.clients.models import ClientContact
from apps.core.tenancy import tenant_context
from apps.cycles.models import EvidenceLink, EvidenceRequest, ExceptionInternal
from apps.cycles.models import Exception as CycleException
from apps.cycles.openers import REQUEST_OVERDUE
from apps.cycles.scanning import FakeScanner, ScanOutcome, scan_intake
from apps.delivery.models import MessageState, OutboundMessage
from apps.delivery.services import report_problem
from apps.tenants.models import TenantRole
from tests.cycles.document_fixtures import reviewed, submit
from tests.cycles.test_exception_navigation import _surface
from tests.cycles.test_exceptions import _open
from tests.delivery.notice_fixtures import acknowledge
from tests.portal.request_fixtures import portal_setup

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("surface", ["cycle", "workspace"])
def test_selected_exception_cannot_escape_client_cycle_or_tenant(surface: str) -> None:
    world = portal_setup("selected-scope")
    foreign = portal_setup("selected-foreign")
    with tenant_context(world.graph.tenant.pk):
        own = _open(world.graph, world.cycle, REQUEST_OVERDUE, world.request.pk)
        sibling = _open(
            world.graph,
            world.other_request.cycle,
            REQUEST_OVERDUE,
            world.other_request.pk,
        )
    with tenant_context(foreign.graph.tenant.pk):
        other = _open(foreign.graph, foreign.cycle, REQUEST_OVERDUE, foreign.request.pk)
    browser = world.firm()
    base = _surface(world, surface)
    separator = "&" if "?" in base else "?"
    selected = f"{base}{separator}pendencia={own.pk}"
    positive = browser.get(selected)
    assert positive.status_code == 200
    assert [row["exception"].pk for row in positive.context["exceptions"]] == [own.pk]
    for identifier in ("", "bad-id", str(uuid4()), str(sibling.pk), str(other.pk)):
        refused = browser.get(f"{base}{separator}pendencia={identifier}")
        assert refused.status_code == 404
    assert world.firm(world.graph.staff).get(selected).status_code == 404
    assert (
        browser.get(
            f"/clientes/{world.graph.client.pk}/?periodo=2026-08&pendencia={own.pk}"
        ).status_code
        == 404
    )


@pytest.mark.parametrize("surface", ["cycle", "workspace"])
def test_document_json_never_authorizes_another_subject_or_arbitrary_url(
    settings: SettingsWrapper, tmp_path: Path, surface: str
) -> None:
    settings.MEDIA_ROOT = tmp_path
    world = portal_setup("references-own")
    foreign = portal_setup("references-foreign")
    intake, link = reviewed(world, "nfe_outra_empresa.xml")
    foreign_intake, _ = reviewed(foreign, "nfe_outra_empresa.xml")
    browser = world.firm()
    path = _surface(world, surface)
    positive = browser.get(path)
    (row,) = positive.context["exceptions"]
    assert row["evidence"][0].url == f"/documentos/{link.pk}/revisar/"
    problem = row["exception"]
    malicious = "https://untrusted.invalid/private"
    for references in (
        [malicious, str(foreign_intake.document.pk), str(uuid4()), 42, None],
        {"url": malicious, "id": str(intake.document.pk)},
        [str(link.pk)],
    ):
        with tenant_context(world.graph.tenant.pk):
            ExceptionInternal.objects.filter(exception=problem).update(
                evidence_refs=references
            )
        response = browser.get(path)
        assert response.status_code == 200
        assert response.context["exceptions"][0]["evidence"] == []
        assert malicious not in response.content.decode()
    with tenant_context(world.graph.tenant.pk):
        ExceptionInternal.objects.filter(exception=problem).update(
            evidence_refs=[str(intake.document.pk)]
        )
        CycleException.objects.filter(pk=problem.pk).update(
            subject_ref=foreign_intake.pk
        )
    assert browser.get(path).context["exceptions"][0]["evidence"] == []
    # A valid UUID in the private JSON is not enough; the intake must match too.
    with tenant_context(world.graph.tenant.pk):
        CycleException.objects.filter(pk=problem.pk).update(subject_ref=intake.pk)
    assert browser.get(path).context["exceptions"][0]["evidence"][0].url == (
        f"/documentos/{link.pk}/revisar/"
    )


@pytest.mark.parametrize("surface", ["cycle", "workspace"])
def test_exception_query_count_does_not_grow_with_rows(surface: str) -> None:
    world = portal_setup("exception-growth")
    with tenant_context(world.graph.tenant.pk):
        _open(world.graph, world.cycle, REQUEST_OVERDUE, world.request.pk)
    browser = world.firm()
    path = _surface(world, surface)
    browser.get(path)  # Warm only session/capability caches before both measurements.
    with CaptureQueriesContext(connection) as small:
        response = browser.get(path)
    assert response.status_code == 200
    assert len(response.context["exceptions"]) == 1
    with tenant_context(world.graph.tenant.pk):
        for index in range(11):
            request = EvidenceRequest.objects.create(
                tenant=world.graph.tenant,
                client=world.graph.client,
                cycle=world.cycle,
                created_by=world.graph.actor,
                title=f"Growth request {index}",
            )
            _open(world.graph, world.cycle, REQUEST_OVERDUE, request.pk)
    with CaptureQueriesContext(connection) as many:
        response = browser.get(path)
    assert response.status_code == 200
    assert len(response.context["exceptions"]) == 12
    assert all(len(row["evidence"]) == 1 for row in response.context["exceptions"])
    assert len(many) <= len(small), (len(small), len(many))


def test_unavailable_scan_has_a_real_review_destination(
    settings: SettingsWrapper, tmp_path: Path
) -> None:
    settings.MEDIA_ROOT = tmp_path
    world = portal_setup("scanner-destination")
    intake = submit(world, "nfe_outra_empresa.xml")
    outcome = scan_intake(
        world.graph.tenant.pk,
        intake.pk,
        scanner=FakeScanner(down=True),
        now=intake.created_at + timedelta(hours=25),
    )
    assert outcome is ScanOutcome.UNAVAILABLE
    with tenant_context(world.graph.tenant.pk):
        link = EvidenceLink.objects.get(document=intake.document)
    browser = world.firm()
    for surface in ("cycle", "workspace"):
        page = browser.get(_surface(world, surface))
        (row,) = page.context["exceptions"]
        assert row["exception"].reason_code == "verificacao_indisponivel"
        (destination,) = row["evidence"]
        assert destination.url == f"/documentos/{link.pk}/revisar/"
        target = browser.get(destination.url)
        assert target.status_code == 200
        assert target.context["link"].pk == link.pk
        assert target.context["document"].pk == intake.document.pk


def test_rejected_invite_matches_its_message_and_ignores_other_references() -> None:
    world = portal_setup("invite-destination")
    world.graph.tenant.features = ["cycles_r1", "import_r1"]
    world.graph.tenant.save(update_fields=["features"])
    acknowledge(world.graph, ("contact_portal_invite",))
    with tenant_context(world.graph.tenant.pk):
        contact = ClientContact.objects.create(
            tenant=world.graph.tenant,
            client=world.graph.client,
            name="New contact",
            email="new@invite-destination.test",
        )
    browser = world.firm()
    queued = browser.post(
        reverse("client-contact-invite", args=[world.graph.client.pk, contact.pk]),
        {"convite-role": TenantRole.CLIENT_OWNER, "convite-version": contact.version},
    )
    assert queued.status_code == 302
    with tenant_context(world.graph.tenant.pk):
        message = OutboundMessage.objects.get(
            kind="contact_portal_invite", contact=contact
        )
        message.state = MessageState.REJECTED
        message.save(update_fields=["state"])
        report_problem(message)
        problem = CycleException.objects.get(reason_code="convite_recusado")
        unrelated = OutboundMessage.objects.get(kind="request_notice")
        ExceptionInternal.objects.filter(exception=problem).update(
            evidence_refs=[str(message.pk), str(unrelated.pk), "https://invalid.test"]
        )
    for surface in ("cycle", "workspace"):
        page = browser.get(_surface(world, surface))
        (row,) = page.context["exceptions"]
        (destination,) = row["evidence"]
        target = browser.get(destination.url)
        assert target.status_code == 200
        assert [item.pk for item in target.context["page"]] == [message.pk]
