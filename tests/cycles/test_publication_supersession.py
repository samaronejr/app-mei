"""Approved revisions preserve the old publication and its exact digest receipt."""

from datetime import timedelta
from uuid import UUID

import pytest
from django.db import DatabaseError, transaction
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.effects.build import BuildError
from apps.cycles.effects.executors import execute
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.models import (
    ActionProposal,
    ClientAcknowledgement,
    ClientPublication,
    CycleEvent,
)
from apps.cycles.services.approvals import approve, propose
from tests.cycles.publication_fixtures import PublicationGraph, publication_graph

pytestmark = pytest.mark.django_db(transaction=True)


def revision_draft(setup: PublicationGraph, target: UUID) -> ActionProposal:
    setup.cycle.refresh_from_db()
    return propose(
        setup.graph.staff,
        setup.cycle.pk,
        ProposalDraft(
            ProposalInput(
                action_kind=ActionKind.CLIENT_PUBLICATION,
                step_key="revised-publication",
                recipient_ids=(setup.contact.pk,),
                subject="Documentação revisada",
                body="As referências do período foram revisadas.",
                supersedes_publication_id=target,
            ),
            timezone.now() + timedelta(days=1),
        ),
        expected_version=setup.cycle.version,
    )


def test_approved_revision_keeps_old_bytes_and_receipt(
    settings: SettingsWrapper,
) -> None:
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    setup = publication_graph("publication-revision")
    with tenant_context(setup.graph.tenant.pk):
        execute(
            setup.graph.actor,
            setup.proposal.pk,
            expected_version=setup.proposal.version,
        )
        old = ClientPublication.objects.get(proposal=setup.proposal)
        original = (old.title, old.body_ptbr, old.publication_digest, old.version)
    response = setup.session().post(
        f"/publicacoes/{old.pk}/", {"publication_digest": old.publication_digest}
    )
    assert response.status_code == 200
    with tenant_context(setup.graph.tenant.pk):
        proposal = revision_draft(setup, old.pk)
        assert not ClientPublication.objects.filter(supersedes=old).exists()
        proposal = approve(
            setup.graph.actor, proposal.pk, expected_version=proposal.version
        ).proposal
        result = execute(
            setup.graph.actor, proposal.pk, expected_version=proposal.version
        )
        assert result.executed
        new = ClientPublication.objects.get(supersedes=old)
        old.refresh_from_db()
        assert (
            old.title,
            old.body_ptbr,
            old.publication_digest,
            old.version,
        ) == original
        receipt = ClientAcknowledgement.objects.get(publication=old)
        assert receipt.publication_digest == original[2]
        assert not ClientAcknowledgement.objects.filter(publication=new).exists()
        with pytest.raises(DatabaseError), transaction.atomic():
            ClientPublication.objects.filter(pk=new.pk).update(supersedes=None)
    page = setup.session().get(f"/publicacoes/{old.pk}/")
    assert page.status_code == 200
    assert f'href="/publicacoes/{new.pk}/"'.encode() in page.content


def test_foreign_publication_cannot_be_frozen_as_revision_target() -> None:
    own = publication_graph("publication-revision-own")
    foreign = publication_graph("publication-revision-other")
    with tenant_context(foreign.graph.tenant.pk):
        execute(
            foreign.graph.actor,
            foreign.proposal.pk,
            expected_version=foreign.proposal.version,
        )
        publication = ClientPublication.objects.get(proposal=foreign.proposal)
    with tenant_context(own.graph.tenant.pk):
        before = (ActionProposal.objects.count(), CycleEvent.objects.count())
        with pytest.raises(BuildError, match="publication_unavailable"):
            revision_draft(own, publication.pk)
        assert (ActionProposal.objects.count(), CycleEvent.objects.count()) == before
