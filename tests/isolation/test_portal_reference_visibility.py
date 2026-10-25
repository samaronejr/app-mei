"""INV-R10 through references: a hidden obligation or document stays hidden one hop on.

Every probe connects as the real `app_portal` role (`portal_cursor` asserts it is not
a superuser and cannot bypass RLS); every hidden case first proves, as the BYPASSRLS
owner, that the row exists, so an empty portal result is the policy, not a missing row.
"""

from collections.abc import Iterable
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import connection, transaction
from django.http import Http404
from django.utils import timezone
from psycopg import sql
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.evidence import (
    DocumentIntake,
    EvidenceLink,
    EvidenceRequest,
    IntakeState,
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


# --- Review 2: a private obligation stays private through every FK chain (B1 class).


def _previous_month_obligation(setup: PortalSetup, *, visible: bool) -> Obligation:
    period = (setup.cycle.period_start - timedelta(days=1)).replace(day=1)
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


def _clear_and_serve(document: Document, payload: bytes) -> None:
    # Scanner-cleared and stored at the served key, so quarantine can never be what
    # hides the row (review 2, B1 reproduction step 3).
    DocumentIntake.objects.filter(document=document).update(state=IntakeState.VERIFIED)
    default_storage.save(document.storage_key, ContentFile(payload))


def _served_staff_document(
    setup: PortalSetup, *, name: str, payload: bytes, visible: bool = True
) -> Document:
    with tenant_context(setup.graph.tenant.pk):
        document, _ = staff_upload(
            setup.graph.actor,
            upload(name, payload),
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            portal_visible=visible,
        )
        _clear_and_serve(document, payload)
    return document


def _staff_link(setup: PortalSetup, document: Document) -> EvidenceLink:
    with tenant_context(setup.graph.tenant.pk):
        return EvidenceLink.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            request=setup.request,
            document=document,
            origin=LinkOrigin.STAFF,
            submitted_by=setup.graph.actor,
        )


def _post_staff_fact(setup: PortalSetup, **fields: str) -> int:
    with tenant_context(setup.graph.tenant.pk):
        version = ServiceCycle.objects.get(pk=setup.cycle.pk).version
    response = setup.firm().post(
        f"/ciclos/{setup.cycle.pk}/",
        {
            "action": "observation",
            "expected_version": version,
            "kind": "payment",
            **fields,
        },
    )
    return response.status_code


def test_reviewer_b1_bound_staff_document_and_its_intake_stay_hidden(
    setup: PortalSetup,
) -> None:
    # Given review 2's B1 reproduction step for step: (1) a same-client hidden
    # obligation the real portal role cannot select, (2) a visible staff upload,
    # (3) the ordinary firm-side ORM binding the two, a verified intake and the bytes
    # at the document's served key.
    hidden = _obligation(setup, visible=False)
    assert _portal_ids(setup, "obligations_obligation", [hidden.pk]) == set()
    payload = PDF + b"PRIVATE-BOUND-DOCUMENT-CANARY"
    with tenant_context(setup.graph.tenant.pk):
        document, _ = staff_upload(
            setup.graph.actor,
            upload("PRIVATE-BOUND-DOCUMENT-CANARY.pdf", payload),
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            portal_visible=True,
        )
        Document.objects.filter(pk=document.pk).update(obligation=hidden)
        _clear_and_serve(document, payload)
        intake = DocumentIntake.objects.get(document=document)
    assert _owner_ids("obligations_document", [document.pk]) == {document.pk}
    assert _owner_ids("cycles_documentintake", [intake.pk]) == {intake.pk}
    # When (4) the real portal role selects the document and its intake, and (5) the
    # vault page and the download are requested.
    with portal_cursor(setup) as cursor:
        cursor.execute(
            "SELECT obligation_id, original_filename FROM obligations_document "
            "WHERE id = %s",
            [document.pk],
        )
        documents = cursor.fetchall()
        cursor.execute(
            "SELECT document_id, state, quarantine_key FROM cycles_documentintake "
            "WHERE document_id = %s",
            [document.pk],
        )
        intakes = cursor.fetchall()
    portal = setup.portal()
    vault = portal.get("/documentos/")
    download = portal.get(f"/documentos/{document.storage_key}")
    # Then neither the document, the hidden parent's id nor the intake is readable.
    assert documents == []
    assert intakes == []
    assert vault.status_code == 200
    assert b"PRIVATE-BOUND-DOCUMENT-CANARY" not in vault.content
    assert download.status_code == 404


@pytest.mark.parametrize("binding", ["private", "visible", "unbound"])
def test_document_intake_link_and_fact_follow_the_bound_obligation(
    setup: PortalSetup, binding: str
) -> None:
    # Given a visible staff document the firm ORM binds to a private or a visible
    # obligation, or leaves unbound (the controls), a firm link to it and a staff fact
    # on a visible subject citing that link.
    subject = _obligation(setup, visible=True)
    payload = PDF + binding.encode()
    document = _served_staff_document(
        setup, name=f"BOUND-{binding}.pdf", payload=payload
    )
    if binding != "unbound":
        target = _previous_month_obligation(setup, visible=binding == "visible")
        with tenant_context(setup.graph.tenant.pk):
            Document.objects.filter(pk=document.pk).update(obligation=target)
    link = _staff_link(setup, document)
    with tenant_context(setup.graph.tenant.pk):
        intake = DocumentIntake.objects.get(document=document)
        fact = record_staff_fact(
            setup.graph.actor,
            subject_id=subject.pk,
            kind="guide_issued",
            period=subject.competence_month,
            source_ref=f"BOUND-FACT-{binding}",
            evidence_link_id=link.pk,
        )
    rows = {
        "obligations_document": document.pk,
        "cycles_documentintake": intake.pk,
        "cycles_evidencelink": link.pk,
        "obligations_sourceobservation": fact.pk,
    }
    for table, pk in rows.items():
        assert _owner_ids(table, [pk]) == {pk}
    # When the real portal role, the vault and the download read them.
    seen = {table: _portal_ids(setup, table, [pk]) for table, pk in rows.items()}
    portal = setup.portal()
    listed = f"BOUND-{binding}.pdf" in portal.get("/documentos/").content.decode()
    download = portal.get(f"/documentos/{document.storage_key}").status_code
    # Then every row one or more hops away follows the obligation, and the visible and
    # unbound controls read exactly as before.
    readable = binding != "private"
    assert seen == {table: ({pk} if readable else set()) for table, pk in rows.items()}
    assert listed is readable
    assert download == (200 if readable else 404)


@pytest.mark.parametrize("evidence_visible", [False, True])
def test_a_correction_inherits_the_privacy_of_the_claim_it_corrects(
    setup: PortalSetup, evidence_visible: bool
) -> None:
    # Given a payment fact the staff HTTP route records with private (or, as the
    # control, visible) evidence, then corrects through the same route.
    subject = _obligation(setup, visible=True)
    evidence = _served_staff_document(
        setup,
        name=f"CLAIM-{evidence_visible}.pdf",
        payload=PDF + f"claim-{evidence_visible}".encode(),
        visible=evidence_visible,
    )
    link = _staff_link(setup, evidence)
    recorded = _post_staff_fact(
        setup,
        op="record",
        subject_id=str(subject.pk),
        source_ref="FIRST-CLAIM",
        evidence_link_id=str(link.pk),
    )
    assert recorded == 302
    with tenant_context(setup.graph.tenant.pk):
        head = SourceObservation.objects.get(subject_ref=subject)
    corrected = _post_staff_fact(
        setup,
        op="correct",
        subject_id=str(subject.pk),
        head_id=str(head.pk),
        source_ref="CORRECTED-CLAIM",
    )
    assert corrected == 302
    with tenant_context(setup.graph.tenant.pk):
        correction = SourceObservation.objects.get(supersedes=head)
    chain = [head.pk, correction.pk]
    assert _owner_ids("obligations_sourceobservation", chain) == set(chain)
    # When the real portal role reads the chain.
    seen = _portal_ids(setup, "obligations_sourceobservation", chain)
    # Then the correction cannot expose a hidden parent; visible evidence reads as
    # before.
    assert seen == (set(chain) if evidence_visible else set())


def test_a_private_correction_does_not_re_elect_the_claim_it_supersedes(
    setup: PortalSetup,
) -> None:
    # Given a visible paid claim recorded through the staff HTTP route and shown on the
    # client's payments page.
    subject = _obligation(setup, visible=True)
    recorded = _post_staff_fact(
        setup, op="record", subject_id=str(subject.pk), source_ref="PAID-CLAIM"
    )
    assert recorded == 302
    with tenant_context(setup.graph.tenant.pk):
        head = SourceObservation.objects.get(subject_ref=subject)
    portal = setup.portal()
    before = portal.get("/pagamentos/").content.decode()
    assert _portal_ids(setup, "obligations_sourceobservation", [head.pk]) == {head.pk}
    # When the firm corrects it through the same route, citing private evidence.
    evidence = _served_staff_document(
        setup, name="PRIVATE-CORRECTION.pdf", payload=PDF + b"private", visible=False
    )
    link = _staff_link(setup, evidence)
    corrected = _post_staff_fact(
        setup,
        op="correct",
        subject_id=str(subject.pk),
        head_id=str(head.pk),
        source_ref="PRIVATE-CORRECTION",
        evidence_link_id=str(link.pk),
    )
    assert corrected == 302
    with tenant_context(setup.graph.tenant.pk):
        correction = SourceObservation.objects.get(supersedes=head)
    chain = [head.pk, correction.pk]
    assert _owner_ids("obligations_sourceobservation", chain) == set(chain)
    after = portal.get("/pagamentos/").content.decode()
    # Then the client reads neither row, and the superseded claim is not re-elected as
    # the current one from what the portal can still see.
    assert _portal_ids(setup, "obligations_sourceobservation", chain) == set()
    marker = 'data-payment-state="manual"'
    assert after.count(marker) == before.count(marker) - 1


def test_a_successor_cannot_expose_a_parent_on_a_private_subject(
    setup: PortalSetup,
) -> None:
    # Given a fact on a private obligation and, written by the firm ORM, a successor
    # filed on a visible obligation: the schema admits a cross-subject chain.
    hidden = _obligation(setup, visible=False)
    visible = _previous_month_obligation(setup, visible=True)
    with tenant_context(setup.graph.tenant.pk):
        parent = record_staff_fact(
            setup.graph.actor,
            subject_id=hidden.pk,
            kind="guide_issued",
            period=hidden.competence_month,
            source_ref="PRIVATE-PARENT",
        )
        successor = SourceObservation.objects.get(pk=parent.pk)
        successor.pk = uuid4()
        successor.subject_ref = visible
        successor.supersedes = parent
        successor.source_ref = "VISIBLE-SUCCESSOR"
        successor.save(force_insert=True)
    chain = [parent.pk, successor.pk]
    assert _owner_ids("obligations_sourceobservation", chain) == set(chain)
    # When the real portal role reads both.
    seen = _portal_ids(setup, "obligations_sourceobservation", chain)
    # Then a visible subject does not make the successor readable over a private parent.
    assert seen == set()


def test_the_chain_function_answers_only_inside_the_ambient_tenant_and_client(
    setup: PortalSetup,
) -> None:
    # Given one fact of this client and, written by the firm ORM, one of a sibling
    # client.
    subject = _obligation(setup, visible=True)
    with tenant_context(setup.graph.tenant.pk):
        fact = record_staff_fact(
            setup.graph.actor,
            subject_id=subject.pk,
            kind="guide_issued",
            period=subject.competence_month,
            source_ref="SCOPED-FACT",
        )
        sibling_subject = Obligation.objects.create(
            tenant=setup.graph.tenant,
            client=setup.other_client,
            obligation_type_id="DAS",
            competence_month=subject.competence_month,
            nominal_due_date=subject.nominal_due_date,
            resolved_due_date=subject.resolved_due_date,
        )
        sibling = SourceObservation.objects.get(pk=fact.pk)
        sibling.pk = uuid4()
        sibling.client = setup.other_client
        sibling.subject_ref = sibling_subject
        sibling.source_ref = "SIBLING-FACT"
        sibling.save(force_insert=True)
    call = "SELECT subject_ref_id FROM obligations_observation_chain(%s)"
    # When app_portal calls the function directly: for its own fact, the sibling's,
    # and with no client in scope.
    with portal_cursor(setup) as cursor:
        cursor.execute(call, [fact.pk])
        own = cursor.fetchall()
        cursor.execute(call, [sibling.pk])
        other = cursor.fetchall()
    with (
        transaction.atomic(),
        tenant_context(setup.graph.tenant.pk),
        connection.cursor() as cursor,
    ):
        cursor.execute("SET LOCAL ROLE app_portal")
        cursor.execute(call, [fact.pk])
        unscoped = cursor.fetchall()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT has_function_privilege(role, "
            "'obligations_observation_chain(uuid)', 'EXECUTE') "
            "FROM unnest(ARRAY['app_portal', 'app_runtime']) AS role"
        )
        executable = [row[0] for row in cursor.fetchall()]
    # Then it lists only the caller's own chain, and only app_portal may call it.
    assert own == [(subject.pk,)]
    assert other == []
    assert unscoped == []
    assert executable == [True, False]


@pytest.mark.parametrize("bound", [True, False])
def test_client_upload_of_bytes_held_in_an_obligation_private_document(
    setup: PortalSetup, bound: bool
) -> None:
    # Given the firm's visible-flag staff document, which the firm ORM binds to a
    # private obligation (or, as the control, leaves unbound).
    payload = PDF + b"shared-bytes"
    held = _served_staff_document(setup, name="firm-copy.pdf", payload=payload)
    if bound:
        hidden = _obligation(setup, visible=False)
        with tenant_context(setup.graph.tenant.pk):
            Document.objects.filter(pk=held.pk).update(obligation=hidden)
    # When the client uploads the same bytes to the vault.
    response = setup.portal().post(
        "/documentos/enviar", {"file": upload("client-copy.pdf", payload)}
    )
    # Then the upload succeeds without touching or revealing the private row: a
    # separate visible original, or the firm's own visible row reused as before.
    assert response.status_code == 302
    with tenant_context(setup.graph.tenant.pk):
        rows = set(
            Document.objects.filter(sha256=held.sha256).values_list("pk", flat=True)
        )
    readable = _portal_ids(setup, "obligations_document", rows)
    if bound:
        assert len(rows) == 2
        assert readable == rows - {held.pk}
    else:
        assert rows == {held.pk}
        assert readable == {held.pk}


PRIVATE_CAPABLE = (
    "obligations_obligation",
    "obligations_document",
    "cycles_documentintake",
    "cycles_evidencerequest",
    "cycles_evidencelink",
    "obligations_sourceobservation",
)
# Every portal-readable FK column into a row an AXRM-012 policy can hide, each folded in
# obligations 0023 and proven hidden in this module (probe names after each pair).
FOLDED_REFERENCES = frozenset(
    {
        # test_reviewer_b1_*, test_document_intake_link_and_fact_*
        ("cycles_documentintake", "document_id"),
        ("obligations_document", "obligation_id"),
        # test_link_and_fact_hide_*, test_document_intake_link_and_fact_*
        ("cycles_evidencelink", "document_id"),
        # test_request_bound_to_a_hidden_obligation_*
        ("cycles_evidencelink", "request_id"),
        ("cycles_evidencerequest", "obligation_id"),
        # test_link_and_fact_hide_*, test_a_correction_*
        ("obligations_sourceobservation", "evidence_link_id"),
        # test_firm_http_fact_*, test_a_successor_cannot_*
        ("obligations_sourceobservation", "subject_ref_id"),
        # test_a_correction_*, test_a_private_correction_*, test_a_successor_cannot_*
        ("obligations_sourceobservation", "supersedes_id"),
    }
)


def test_every_portal_readable_reference_to_a_private_capable_row_is_folded() -> None:
    # Given the live catalog: each FK column on a table app_portal can SELECT that
    # points at a row an AXRM-012 policy can hide.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT DISTINCT fk.conrelid::regclass::text, attribute.attname "
            "FROM pg_constraint fk JOIN pg_attribute attribute "
            "ON attribute.attrelid = fk.conrelid "
            "AND attribute.attnum = ANY (fk.conkey) "
            "WHERE fk.contype = 'f' AND fk.confrelid::regclass::text = ANY (%s) "
            "AND has_table_privilege('app_portal', fk.conrelid, 'SELECT') "
            "AND attribute.attname NOT IN ('tenant_id', 'client_id')",
            [list(PRIVATE_CAPABLE)],
        )
        found = set(cursor.fetchall())
    # Then each one is registered with its fold and its real-role probe; a new reference
    # fails here until it is folded and proven.
    assert found == FOLDED_REFERENCES
