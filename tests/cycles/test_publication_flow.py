"""Approval, actual execution, authenticated reading and exact-version receipt."""

import pytest
from django.test import Client
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.effects.executors import execute
from apps.cycles.errors import VersionConflict
from apps.cycles.models import (
    ActionExecution,
    ClientAcknowledgement,
    ClientPublication,
    CycleEvent,
)
from apps.delivery.models import OutboundMessage
from apps.tenants.models import TenantRole
from tests.cycles.publication_fixtures import publication_graph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    "role", [TenantRole.CLIENT_OWNER, TenantRole.CLIENT_COLLABORATOR]
)
def test_approved_publication_is_viewed_then_explicitly_acknowledged(
    settings: SettingsWrapper, role: str
) -> None:

    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    setup = publication_graph(role=role)
    with tenant_context(setup.graph.tenant.pk):
        result = execute(setup.graph.actor, setup.proposal.pk, expected_version=2)
        assert result.executed
        publication = ClientPublication.objects.get(proposal=setup.proposal)
        message = OutboundMessage.objects.get(proposal_id=setup.proposal.pk)
        assert message.recipients_json == [setup.portal_user.email]
        assert publication.publication_digest == setup.proposal.digest
        assert not ClientAcknowledgement.objects.exists()
    browser = setup.session()
    url = f"/publicacoes/{publication.pk}/"
    viewed = browser.get(url)
    assert viewed.status_code == 200
    assert b"&lt;script&gt;" in viewed.content
    assert b"<script>window.T16_CANARY" not in viewed.content
    with tenant_context(setup.graph.tenant.pk):
        assert not ClientAcknowledgement.objects.exists()
        assert (
            CycleEvent.objects.filter(
                aggregate_id=publication.pk, type="publication.viewed"
            ).count()
            == 1
        )
    posted = browser.post(url, {"publication_digest": publication.publication_digest})
    assert posted.status_code in {200, 302}
    with tenant_context(setup.graph.tenant.pk):
        receipt = ClientAcknowledgement.objects.get(publication=publication)
        assert receipt.user_id == setup.portal_user.pk
        assert receipt.publication_digest == setup.proposal.digest
    repeated = browser.post(url, {"publication_digest": publication.publication_digest})
    assert repeated.status_code in {200, 302}
    with tenant_context(setup.graph.tenant.pk):
        assert (
            ClientAcknowledgement.objects.filter(publication=publication).count() == 1
        )


def test_receipt_duplicate_from_a_fresh_session_reports_the_conflict(
    settings: SettingsWrapper,
) -> None:
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    setup = publication_graph()
    with tenant_context(setup.graph.tenant.pk):
        execute(setup.graph.actor, setup.proposal.pk, expected_version=2)
        publication = ClientPublication.objects.get(proposal=setup.proposal)
    url = f"/publicacoes/{publication.pk}/"
    payload = {"publication_digest": publication.publication_digest}
    assert setup.session().post(url, payload).status_code == 200
    duplicate = setup.session().post(url, payload)
    assert duplicate.status_code == 409
    assert (
        f'data-receipt-digest="{publication.publication_digest}"'.encode()
        in duplicate.content
    )
    with tenant_context(setup.graph.tenant.pk):
        assert (
            ClientAcknowledgement.objects.filter(publication=publication).count() == 1
        )


@pytest.mark.parametrize("request_kind", ["head", "prefetch", "anonymous"])
def test_noninteractive_fetch_cannot_record_view_or_receipt(
    settings: SettingsWrapper, request_kind: str
) -> None:

    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    setup = publication_graph()
    with tenant_context(setup.graph.tenant.pk):
        execute(setup.graph.actor, setup.proposal.pk, expected_version=2)
        publication = ClientPublication.objects.get(proposal=setup.proposal)
    browser = setup.session()
    url = f"/publicacoes/{publication.pk}/"
    if request_kind == "head":
        response = browser.head(url)
    elif request_kind == "prefetch":
        response = browser.get(url, HTTP_SEC_PURPOSE="prefetch")
    else:
        response = Client(
            SERVER_NAME=f"{setup.graph.tenant.slug}-portal.localhost"
        ).get(url)
    assert response.status_code in {200, 302}
    with tenant_context(setup.graph.tenant.pk):
        assert not ClientAcknowledgement.objects.exists()
        assert not CycleEvent.objects.filter(
            aggregate_id=publication.pk, type="publication.viewed"
        ).exists()


@pytest.mark.parametrize("digest", ["", "not-a-digest", "0" * 64])
def test_receipt_cannot_confirm_different_publication_bytes(
    settings: SettingsWrapper, digest: str
) -> None:

    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    setup = publication_graph()
    with tenant_context(setup.graph.tenant.pk):
        execute(setup.graph.actor, setup.proposal.pk, expected_version=2)
        publication = ClientPublication.objects.get(proposal=setup.proposal)
    response = setup.session().post(
        f"/publicacoes/{publication.pk}/", {"publication_digest": digest}
    )
    assert response.status_code in {409, 422}
    with tenant_context(setup.graph.tenant.pk):
        assert not ClientAcknowledgement.objects.exists()


def test_stale_executor_form_writes_nothing() -> None:

    setup = publication_graph()
    with tenant_context(setup.graph.tenant.pk):
        before = CycleEvent.objects.count()
        with pytest.raises(VersionConflict):
            execute(setup.graph.actor, setup.proposal.pk, expected_version=1)
        assert not ActionExecution.objects.exists()
        assert not ClientPublication.objects.exists()
        assert not OutboundMessage.objects.exists()
        assert CycleEvent.objects.count() == before


def test_second_executor_caller_cannot_replay_a_stale_rendered_version() -> None:
    setup = publication_graph()
    with tenant_context(setup.graph.tenant.pk):
        execute(setup.graph.actor, setup.proposal.pk, expected_version=2)
        before = CycleEvent.objects.count()
        with pytest.raises(VersionConflict):
            execute(setup.graph.staff, setup.proposal.pk, expected_version=2)
        assert ActionExecution.objects.count() == 1
        assert ClientPublication.objects.count() == 1
        assert OutboundMessage.objects.count() == 1
        assert CycleEvent.objects.count() == before
