"""A publication only serves its selected, unchanged, clean reviewed document."""

from datetime import timedelta
from pathlib import Path

import pytest
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.clients.models import ClientAssignment
from apps.core.tenancy import tenant_context
from apps.cycles.effects.executors import execute
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.models import ClientPublication, EvidenceLink, ReviewDecision
from apps.cycles.services.approvals import approve, propose
from apps.cycles.services.review import record_review
from apps.tenants.models import Membership
from tests.cycles.document_fixtures import reviewed
from tests.portal.request_fixtures import portal_setup

pytestmark = pytest.mark.django_db(transaction=True)


def test_publication_download_is_app_mediated_scoped_and_filename_safe(
    settings: SettingsWrapper, tmp_path: Path
) -> None:

    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    settings.MEDIA_ROOT = tmp_path
    setup = portal_setup("publication-download")
    intake, link = reviewed(setup, "nfe_emitente_cliente.xml")
    with tenant_context(setup.graph.tenant.pk):
        ClientAssignment.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            user=setup.graph.staff,
        )
        setup.contact.membership = Membership.objects.get(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            user=setup.portal_user,
        )
        setup.contact.save(update_fields=["membership"])
        setup.request.refresh_from_db()
        record_review(
            setup.graph.actor,
            link.pk,
            decision=ReviewDecision.ACCEPTED,
            client_reason="",
            corrections=[],
            expected_version=setup.request.version,
        )
        document = intake.document
        document.original_filename = '../../<script>"hostile".xml'
        document.save(update_fields=["original_filename"])
        setup.cycle.refresh_from_db()
        proposal = propose(
            setup.graph.staff,
            setup.cycle.pk,
            ProposalDraft(
                ProposalInput(
                    action_kind=ActionKind.CLIENT_PUBLICATION,
                    step_key="publication-download",
                    recipient_ids=(setup.contact.pk,),
                    document_link_ids=(link.pk,),
                    subject="Arquivo conferido",
                    body="Confira o documento publicado.",
                ),
                timezone.now() + timedelta(days=1),
            ),
            expected_version=setup.cycle.version,
        )
        approve(setup.graph.actor, proposal.pk, expected_version=1)
        execute(setup.graph.actor, proposal.pk, expected_version=2)
        publication = ClientPublication.objects.get(proposal=proposal)
    portal = setup.portal()
    url = f"/publicacoes/{publication.pk}/documentos/{link.pk}/"
    response = portal.get(url)
    assert response.status_code == 200
    assert not response.streaming
    assert response["Content-Disposition"].startswith("attachment;")
    assert "\r" not in response["Content-Disposition"]
    assert "\n" not in response["Content-Disposition"]
    assert response.content.startswith(b"<?xml")
    assert (
        portal.get(
            f"/publicacoes/{publication.pk}/documentos/{setup.other_request.pk}/"
        ).status_code
        == 404
    )
    with tenant_context(setup.graph.tenant.pk):
        EvidenceLink.objects.filter(pk=link.pk).update(client_reason="Nova revisão")
    assert portal.get(url).status_code == 404
