"""INV-R10 through references: a hidden obligation or document stays hidden one hop on.

Every probe connects as the real `app_portal` role (`portal_cursor` asserts it is not
a superuser and cannot bypass RLS); every hidden case first proves, as the BYPASSRLS
owner, that the row exists, so an empty portal result is the policy, not a missing row.
"""

from collections.abc import Iterable
from pathlib import Path
from uuid import UUID

import pytest
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.http import Http404
from django.utils import timezone
from psycopg import sql
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.evidence import (
    EvidenceLink,
    EvidenceRequest,
    LinkOrigin,
    RequestKind,
    RequestState,
)
from apps.cycles.intake import staff_upload, vault_upload
from apps.cycles.models import ServiceCycle
from apps.cycles.services.requests import PRIVATE_OBLIGATION, send_requests
from apps.obligations.models import Document, Obligation, SourceObservation
from apps.obligations.observations import record_staff_fact
from apps.obligations.tasks import materialize_receipt_observations
from tests.obligations.ingest_fixtures import PDF, ingest_setup, portal_cursor
from tests.portal.request_fixtures import PortalSetup, upload

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def setup(settings: SettingsWrapper, tmp_path: Path) -> PortalSetup:
    return ingest_setup(settings, tmp_path)


def _obligation(setup: PortalSetup, *, visible: bool) -> Obligation:
    period = setup.cycle.period_start
    with tenant_context(setup.graph.tenant.pk):
        return Obligation.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            obligation_type_id="DAS",
            competence_month=period,
            nominal_due_date=period,
            resolved_due_date=period,
            portal_visible=visible,
        )


def _select(table: str) -> sql.Composed:
    return sql.SQL("SELECT id FROM {} WHERE id = ANY(%s)").format(sql.Identifier(table))


def _owner_ids(table: str, ids: Iterable[UUID]) -> set[UUID]:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
        cursor.execute(_select(table), [list(ids)])
        found = {row[0] for row in cursor.fetchall()}
        transaction.set_rollback(True)
    return found


def _portal_ids(setup: PortalSetup, table: str, ids: Iterable[UUID]) -> set[UUID]:
    with portal_cursor(setup) as cursor:
        cursor.execute(_select(table), [list(ids)])
        return {row[0] for row in cursor.fetchall()}


@pytest.mark.parametrize("visible", [False, True])
def test_firm_http_fact_follows_its_subjects_visibility(
    setup: PortalSetup, visible: bool
) -> None:
    # Given a hidden (or, as the control, visible) obligation of this cycle.
    subject = _obligation(setup, visible=visible)
    # When the firm records a payment fact through the real cycle route.
    response = setup.firm().post(
        f"/ciclos/{setup.cycle.pk}/",
        {
            "action": "observation",
            "expected_version": setup.cycle.version,
            "op": "record",
            "subject_id": str(subject.pk),
            "kind": "payment",
            "source_ref": "FIRM-PROTOCOL-CANARY",
        },
    )
    assert response.status_code == 302
    with tenant_context(setup.graph.tenant.pk):
        observation = SourceObservation.objects.get(subject_ref=subject)
    assert _owner_ids("obligations_sourceobservation", [observation.pk]) == {
        observation.pk
    }
    # Then the portal reads the protocol only when the subject itself is visible.
    with portal_cursor(setup) as cursor:
        cursor.execute(
            "SELECT count(*), max(source_ref) FROM obligations_sourceobservation "
            "WHERE subject_ref_id = %s",
            [subject.pk],
        )
        expected = (1, "FIRM-PROTOCOL-CANARY") if visible else (0, None)
        assert cursor.fetchone() == expected


@pytest.mark.parametrize("document_visible", [False, True])
def test_link_and_fact_hide_with_their_evidence_document(
    setup: PortalSetup, document_visible: bool
) -> None:
    # Given a VISIBLE subject whose staff fact cites a link to a hidden original.
    subject = _obligation(setup, visible=True)
    with tenant_context(setup.graph.tenant.pk):
        document, _ = staff_upload(
            setup.graph.actor,
            upload("evidence.pdf", PDF + b"evidence"),
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            portal_visible=document_visible,
        )
        link = EvidenceLink.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            request=setup.request,
            document=document,
            origin=LinkOrigin.STAFF,
            submitted_by=setup.graph.actor,
            client_reason="PRIVATE-DOCUMENT-REVIEW-CANARY",
        )
        fact = record_staff_fact(
            setup.graph.actor,
            subject_id=subject.pk,
            kind="guide_issued",
            period=subject.competence_month,
            source_ref="EVIDENCE-SOURCE-CANARY",
            evidence_link_id=link.pk,
        )
    assert _owner_ids("cycles_evidencelink", [link.pk]) == {link.pk}
    assert _owner_ids("obligations_sourceobservation", [fact.pk]) == {fact.pk}
    # When the real portal role reads the link and the fact that cites it.
    links = _portal_ids(setup, "cycles_evidencelink", [link.pk])
    facts = _portal_ids(setup, "obligations_sourceobservation", [fact.pk])
    # Then both follow the document, although subject and request are visible.
    assert links == ({link.pk} if document_visible else set())
    assert facts == ({fact.pk} if document_visible else set())


def test_request_bound_to_a_hidden_obligation_is_unreadable_and_unanswerable(
    setup: PortalSetup,
) -> None:
    # Given a sent receipt request bound to a hidden obligation (written directly:
    # the request services now refuse that binding), a visible client file linked
    # to it, and the existing materializer's observation of that link.
    hidden = _obligation(setup, visible=False)
    with tenant_context(setup.graph.tenant.pk):
        request = EvidenceRequest.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            cycle=setup.cycle,
            kind=RequestKind.COMPROVANTE,
            obligation=hidden,
            title="PRIVATE-REQUEST-TITLE",
            client_note="PRIVATE-REQUEST-NOTE",
            state=RequestState.REQUESTED,
            requested_at=timezone.now(),
            created_by=setup.graph.actor,
        )
        document, _ = staff_upload(
            setup.graph.actor,
            upload("receipt.pdf", PDF + b"receipt"),
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            portal_visible=True,
        )
        link = EvidenceLink.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            request=request,
            document=document,
            origin=LinkOrigin.PORTAL,
            submitted_by=setup.portal_user,
        )
    assert materialize_receipt_observations.apply().get() >= 1
    with tenant_context(setup.graph.tenant.pk):
        submitted = SourceObservation.objects.get(evidence_link=link)
    assert _owner_ids("cycles_evidencerequest", [request.pk]) == {request.pk}
    # When the real portal role reads the request, its link and its observation.
    requests = _portal_ids(
        setup, "cycles_evidencerequest", [request.pk, setup.request.pk]
    )
    links = _portal_ids(setup, "cycles_evidencelink", [link.pk])
    facts = _portal_ids(setup, "obligations_sourceobservation", [submitted.pk])
    # Then only the fixture's ordinary request survives; nothing bound to the
    # hidden obligation is readable, served or answerable.
    assert requests == {setup.request.pk}
    assert links == set()
    assert facts == set()
    portal = setup.portal()
    assert portal.get(f"/solicitacoes/{setup.request.pk}/").status_code == 200
    page = portal.get(f"/solicitacoes/{request.pk}/")
    assert page.status_code == 404
    assert b"PRIVATE-REQUEST-TITLE" not in page.content
    answer = portal.post(
        f"/solicitacoes/{request.pk}/enviar",
        {"file": upload("answer.pdf", PDF + b"answer")},
    )
    assert answer.status_code == 404
    with tenant_context(setup.graph.tenant.pk):
        assert EvidenceLink.objects.filter(request=request).count() == 1
        assert not Document.objects.filter(original_filename="answer.pdf").exists()


@pytest.mark.parametrize("visible", [False, True])
def test_firm_route_binds_a_receipt_request_only_to_a_visible_obligation(
    setup: PortalSetup, visible: bool
) -> None:
    # Given a hidden (or, as the control, visible) obligation of this cycle.
    obligation = _obligation(setup, visible=visible)
    # When the firm creates a receipt request through the existing route.
    response = setup.firm().post(
        f"/ciclos/{setup.cycle.pk}/solicitacoes/",
        {
            "action": "create",
            "expected_version": setup.cycle.version,
            "title": "Comprovante do DAS",
            "client_note": "Envie o comprovante.",
            "kind": "comprovante",
            "obligation_id": str(obligation.pk),
        },
    )
    # Then a private obligation is refused with nothing written.
    with tenant_context(setup.graph.tenant.pk):
        bound = EvidenceRequest.objects.filter(obligation=obligation)
        if visible:
            assert response.status_code == 302
            assert bound.count() == 1
        else:
            assert response.status_code == 422
            assert PRIVATE_OBLIGATION in response.content.decode()
            assert not bound.exists()


def test_send_refuses_a_draft_bound_to_a_hidden_obligation(setup: PortalSetup) -> None:
    # Given a draft that bypassed the binding check (written directly).
    hidden = _obligation(setup, visible=False)
    with tenant_context(setup.graph.tenant.pk):
        draft = EvidenceRequest.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            cycle=setup.cycle,
            kind=RequestKind.COMPROVANTE,
            obligation=hidden,
            title="PRIVATE-DRAFT-TITLE",
            created_by=setup.graph.actor,
        )
        cycle = ServiceCycle.objects.get(pk=setup.cycle.pk)
        # When the firm sends the cycle's drafts.
        with pytest.raises(ValidationError) as refused:
            send_requests(setup.graph.actor, cycle.pk, expected_version=cycle.version)
        # Then nothing is published or versioned.
        assert PRIVATE_OBLIGATION in refused.value.messages
        draft.refresh_from_db()
        assert draft.state == RequestState.DRAFT
        assert ServiceCycle.objects.get(pk=cycle.pk).version == cycle.version


@pytest.mark.parametrize("visible", [False, True])
def test_vault_upload_binds_only_a_visible_obligation(
    setup: PortalSetup, visible: bool
) -> None:
    # Given a hidden (or, as the control, visible) same-client obligation id.
    obligation = _obligation(setup, visible=visible)
    # When the client posts it with an upload, as only a forged form could.
    response = setup.portal().post(
        "/documentos/enviar",
        {"file": upload("bound.pdf", PDF + b"bound"), "obligation": str(obligation.pk)},
    )
    # Then a private obligation is a 404 before any byte or row exists.
    with tenant_context(setup.graph.tenant.pk):
        stored = Document.objects.filter(original_filename="bound.pdf")
        if visible:
            assert response.status_code == 302
            assert [row.obligation_id for row in stored] == [obligation.pk]
        else:
            assert response.status_code == 404
            assert not stored.exists()


def test_vault_service_refuses_a_hidden_obligation_without_portal_rls(
    setup: PortalSetup,
) -> None:
    # Given the firm-side connection, where RLS shows every obligation of the tenant.
    hidden = _obligation(setup, visible=False)
    with tenant_context(setup.graph.tenant.pk):
        # When the vault service is asked to bind the hidden obligation.
        with pytest.raises(Http404):
            vault_upload(
                setup.portal_user,
                upload("service.pdf", PDF + b"service"),
                tenant=setup.graph.tenant,
                client=setup.graph.client,
                obligation_id=hidden.pk,
            )
        # Then its own check refused it; nothing was stored.
        assert not Document.objects.filter(original_filename="service.pdf").exists()
