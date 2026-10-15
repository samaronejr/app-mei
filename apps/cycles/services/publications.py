"""Withdrawal is a new approved effect in the existing client_publication class."""

from datetime import timedelta
from uuid import UUID

from django.db import transaction
from django.http import Http404
from django.utils import timezone

from apps.accounts.models import User
from apps.clients.maintenance import load_client
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.models import ActionProposal, ClientPublication
from apps.cycles.services.approvals import propose
from apps.cycles.services.cycle import check_version, tenant_id


@transaction.atomic
def propose_withdrawal(
    actor: User, publication_id: UUID, *, expected_version: int, reason: str
) -> ActionProposal:
    """Freeze the target under propose's authority/client/cycle/source lock order."""
    publication = ClientPublication.objects.filter(pk=publication_id).first()
    if publication is None:
        raise Http404
    load_client(actor, tenant_id(), publication.client_id)
    # Fail stale forms before even constructing a proposal; the same version is
    # checked again after propose has acquired the target lock.
    check_version(publication.version, expected_version)
    original = publication.proposal
    selected = ProposalInput.from_data(original.input_spec)
    proposal = propose(
        actor,
        publication.cycle_id,
        ProposalDraft(
            ProposalInput(
                action_kind=ActionKind.CLIENT_PUBLICATION,
                step_key=f"withdraw:{publication.business_intent_key}",
                recipient_ids=selected.recipient_ids,
                subject="Retirar publicação",
                body="Esta publicação será retirada do portal pelo escritório.",
                reason=reason,
                withdraw_publication_id=publication.pk,
            ),
            timezone.now() + timedelta(hours=72),
        ),
        expected_version=publication.cycle.version,
    )
    publication.refresh_from_db()
    check_version(publication.version, expected_version)
    return proposal
