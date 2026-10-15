"""A real fake SMTP peer cannot produce or erase an explicit portal receipt."""

from pathlib import Path

import pytest
from django.test import override_settings
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.effects.executors import execute
from apps.cycles.models import ClientAcknowledgement, ClientPublication
from apps.delivery.models import MessageState, OutboundMessage
from apps.delivery.tasks import dispatch_tenant
from tests.cycles.publication_fixtures import publication_graph
from tests.delivery.smtp import FakeSMTP

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    ("mode", "outcome"),
    [
        ("accept", MessageState.ACCEPTED),
        ("reject", MessageState.REJECTED),
        ("disconnect", MessageState.UNKNOWN),
    ],
)
def test_explicit_digest_receipt_is_independent_of_smtp_outcome(
    settings: SettingsWrapper, tmp_path: Path, mode: str, outcome: str
) -> None:

    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    settings.DELIVERY_PAUSED = False
    settings.MEDIA_ROOT = tmp_path
    with (
        FakeSMTP(mode=mode) as peer,
        override_settings(
            EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
            EMAIL_HOST="127.0.0.1",
            EMAIL_PORT=peer.port,
            EMAIL_HOST_USER="",
            EMAIL_HOST_PASSWORD="",
            EMAIL_USE_TLS=False,
            EMAIL_USE_SSL=False,
            EMAIL_TIMEOUT=3,
        ),
    ):
        setup = publication_graph()
        with tenant_context(setup.graph.tenant.pk):
            execute(setup.graph.actor, setup.proposal.pk, expected_version=2)
            publication = ClientPublication.objects.get(proposal=setup.proposal)
        assert dispatch_tenant(str(setup.graph.tenant.pk)) == 1
        with tenant_context(setup.graph.tenant.pk):
            message = OutboundMessage.objects.get(proposal_id=setup.proposal.pk)
            assert message.state == outcome
            assert not ClientAcknowledgement.objects.exists()
        assert len(peer.received) == 1
        response = setup.session().post(
            f"/publicacoes/{publication.pk}/",
            {"publication_digest": publication.publication_digest},
        )
        assert response.status_code in {200, 302}
        with tenant_context(setup.graph.tenant.pk):
            receipt = ClientAcknowledgement.objects.get(publication=publication)
            assert receipt.publication_digest == setup.proposal.digest
            assert receipt.user_id == setup.portal_user.pk
            message.refresh_from_db()
            assert message.state == outcome
        assert dispatch_tenant(str(setup.graph.tenant.pk)) == 0
        assert len(peer.received) == 1
