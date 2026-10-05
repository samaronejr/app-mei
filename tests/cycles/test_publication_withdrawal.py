"""Withdrawal is another exact approved publication effect, never an edit bypass."""

import json
from pathlib import Path

import pytest
from pytest_django.fixtures import SettingsWrapper

from apps.clients.maintenance import ContactData, save_contact
from apps.core.tenancy import tenant_context
from apps.cycles.effects.executors import execute
from apps.cycles.errors import VersionConflict
from apps.cycles.models import ActionProposal, ClientAcknowledgement, ClientPublication
from apps.cycles.services.approvals import approve
from apps.cycles.services.publications import propose_withdrawal
from apps.delivery.models import MessageState, OutboundMessage
from apps.delivery.tasks import dispatch_tenant
from tests.cycles.publication_fixtures import publication_graph
from tests.delivery.smtp import FakeSMTP

pytestmark = pytest.mark.django_db(transaction=True)


def test_withdrawal_form_announces_invalid_reason_without_creating_proposal(
    settings: SettingsWrapper,
) -> None:
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    setup = publication_graph()
    with tenant_context(setup.graph.tenant.pk):
        execute(setup.graph.actor, setup.proposal.pk, expected_version=2)
        publication = ClientPublication.objects.get(proposal=setup.proposal)
        before = ActionProposal.objects.count()
    response = setup.session(portal=False).post(
        f"/publicacoes/{publication.pk}/retirar/",
        {"expected_version": publication.version, "reason": ""},
    )
    assert response.status_code == 422
    assert b'class="form-errors"' in response.content
    assert b'href="#id_reason"' in response.content
    assert b'aria-invalid="true"' in response.content
    with tenant_context(setup.graph.tenant.pk):
        publication.refresh_from_db()
        assert publication.state == "publicada"
        assert ActionProposal.objects.count() == before


def test_withdrawal_needs_new_approval_and_preserves_exact_receipt_history(
    settings: SettingsWrapper,
) -> None:

    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    setup = publication_graph()
    with tenant_context(setup.graph.tenant.pk):
        execute(setup.graph.actor, setup.proposal.pk, expected_version=2)
        publication = ClientPublication.objects.get(proposal=setup.proposal)
    portal = setup.session()
    url = f"/publicacoes/{publication.pk}/"
    assert portal.post(
        url, {"publication_digest": publication.publication_digest}
    ).status_code in {200, 302}
    with tenant_context(setup.graph.tenant.pk):
        withdrawn = propose_withdrawal(
            setup.graph.staff,
            publication.pk,
            expected_version=publication.version,
            reason="A publicação precisa ser retirada.",
        )
        assert withdrawn.action_kind == "client_publication"
        frozen = json.loads(bytes(withdrawn.manifest_bytes))
        assert frozen["payload"]["withdrawal"]["publication_id"] == str(publication.pk)
        assert (
            frozen["payload"]["withdrawal"]["digest"] == publication.publication_digest
        )
        assert not execute(setup.graph.actor, withdrawn.pk, expected_version=1).executed
        publication.refresh_from_db()
        assert publication.state == "publicada"
        approve(setup.graph.actor, withdrawn.pk, expected_version=1)
        assert execute(setup.graph.actor, withdrawn.pk, expected_version=2).executed
        publication.refresh_from_db()
        assert publication.state == "retirada"
        assert publication.withdrawn_at is not None
        assert publication.publication_digest == setup.proposal.digest
        assert ClientAcknowledgement.objects.get(
            publication=publication
        ).publication_digest == (setup.proposal.digest)
        assert OutboundMessage.objects.filter(proposal_id=withdrawn.pk).count() == 1
    assert (
        portal.post(
            url, {"publication_digest": publication.publication_digest}
        ).status_code
        == 409
    )
    with tenant_context(setup.graph.tenant.pk):
        assert (
            ClientAcknowledgement.objects.filter(publication=publication).count() == 1
        )
        before = OutboundMessage.objects.count()
        with pytest.raises(VersionConflict):
            propose_withdrawal(
                setup.graph.staff,
                publication.pk,
                expected_version=1,
                reason="Stale withdrawal.",
            )
        assert OutboundMessage.objects.count() == before


@pytest.mark.parametrize("recipient_changed", [False, True])
def test_withdrawal_notice_revalidates_after_its_own_completed_effect(
    settings: SettingsWrapper, tmp_path: Path, recipient_changed: bool
) -> None:
    settings.DELIVERY_PAUSED = False
    settings.MEDIA_ROOT = tmp_path
    settings.EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    settings.EMAIL_HOST = "127.0.0.1"
    settings.EMAIL_HOST_USER = ""
    settings.EMAIL_HOST_PASSWORD = ""
    settings.EMAIL_USE_TLS = False
    settings.EMAIL_USE_SSL = False
    settings.EMAIL_TIMEOUT = 3
    with FakeSMTP(mode="accept") as peer:
        settings.EMAIL_PORT = peer.port
        setup = publication_graph("withdrawal-dispatch")
        with tenant_context(setup.graph.tenant.pk):
            execute(setup.graph.actor, setup.proposal.pk, expected_version=2)
            publication = ClientPublication.objects.get(proposal=setup.proposal)
            withdrawal = propose_withdrawal(
                setup.graph.staff,
                publication.pk,
                expected_version=publication.version,
                reason="Retirar a versão publicada.",
            )
            approve(setup.graph.actor, withdrawal.pk, expected_version=1)
            execute(setup.graph.actor, withdrawal.pk, expected_version=2)
            if recipient_changed:
                save_contact(
                    actor=setup.graph.actor,
                    tenant_id=setup.graph.tenant.pk,
                    client_id=setup.graph.client.pk,
                    contact_id=setup.contact.pk,
                    data=ContactData(
                        name=setup.contact.name, email="changed@example.test"
                    ),
                    expected_version=setup.contact.version,
                )
        dispatch_tenant(str(setup.graph.tenant.pk))
        with tenant_context(setup.graph.tenant.pk):
            notice = OutboundMessage.objects.get(proposal_id=withdrawal.pk)
            assert notice.state == (
                MessageState.CANCELLED if recipient_changed else MessageState.ACCEPTED
            )
            assert (
                OutboundMessage.objects.get(proposal_id=setup.proposal.pk).state
                == MessageState.CANCELLED
            )
        assert len(peer.received) == (0 if recipient_changed else 1)
