"""Pending exact-version receipts drive the client action, under real portal RLS."""

from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.cycles.effects.executors import execute
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.models import ClientAcknowledgement, ClientPublication
from apps.cycles.services.approvals import approve, propose
from apps.tenants.models import TenantRole
from ops.perf.results import GZIP_BYTES
from tests.cycles.publication_fixtures import publication_graph
from tests.portal.test_portal_next_action import Markup
from tests.ui.factories import add_member

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _portal_surface(settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.ALLOWED_HOSTS = [".localhost", "localhost", "testserver"]
    monkeypatch.setattr("apps.portal.middleware.PORTAL_URLCONF", "apps.portal.urls")


@pytest.mark.parametrize(
    "role", [TenantRole.CLIENT_OWNER, TenantRole.CLIENT_COLLABORATOR]
)
def test_publication_is_next_action_until_exact_receipt_is_confirmed(role: str) -> None:
    # Given an approved, actually executed publication and no request work.
    setup = publication_graph("next-publication", role=role)
    with tenant_context(setup.graph.tenant.pk):
        assert execute(
            setup.graph.actor, setup.proposal.pk, expected_version=2
        ).executed
        publication = ClientPublication.objects.get(proposal=setup.proposal)
    browser = setup.session()

    # When the signed-in recipient opens Inicio.
    with CaptureQueriesContext(connection) as queries:
        response = browser.get("/")

    # Then the bounded read offers the real confirmation page.
    assert response.status_code == 200
    markup = Markup(response.content)
    assert markup.one("id", "portal-status")["data-status"] == "action_required"
    assert (
        markup.one("aria-labelledby", "proxima-acao")["data-action-kind"]
        == "publication"
    )
    link = markup.one("data-primary-action", "publication")
    url = f"/publicacoes/{publication.pk}/"
    assert link["href"] == url
    markup.one("data-publication-pending", str(publication.pk))
    assert len(queries) <= 25
    assert len(response.content) < GZIP_BYTES
    assert browser.get(url).status_code == 200
    assert (
        browser.post(
            url, {"publication_digest": publication.publication_digest}
        ).status_code
        == 200
    )
    with tenant_context(setup.graph.tenant.pk):
        assert ClientAcknowledgement.objects.filter(
            publication=publication,
            user=setup.portal_user,
            publication_digest=publication.publication_digest,
        ).exists()
    # Both this session and a new sign-in must see the cleared action.
    for session in (browser, setup.session()):
        cleared = session.get("/")
        assert cleared.status_code == 200
        assert b"data-publication-pending" not in cleared.content
        assert (
            Markup(cleared.content).one("aria-labelledby", "proxima-acao")[
                "data-action-kind"
            ]
            == "none"
        )


def test_another_client_cannot_see_or_confirm_the_publication() -> None:
    # Given a published first client's item and a second client seat in the same firm.
    setup = publication_graph("publication-foreign")
    with tenant_context(setup.graph.tenant.pk):
        execute(setup.graph.actor, setup.proposal.pk, expected_version=2)
        publication = ClientPublication.objects.get(proposal=setup.proposal)
        other = ClientCompany.objects.create(
            tenant=setup.graph.tenant,
            legal_name="Another client",
            cnpj="11444777000161",
            status="active",
        )
    user = add_member(
        setup.graph.tenant,
        "other@publication-foreign.test",
        TenantRole.CLIENT_OWNER,
        client=other,
    )

    # When that other client visits Inicio and guesses the exact confirmation URL.
    browser = setup.session(user=user)
    response = browser.get("/")
    url = f"/publicacoes/{publication.pk}/"

    # Then neither the pending action nor the publication bytes cross the boundary.
    assert response.status_code == 200
    assert str(publication.pk).encode() not in response.content
    assert publication.title.encode() not in response.content
    assert (
        Markup(response.content).one("aria-labelledby", "proxima-acao")[
            "data-action-kind"
        ]
        == "none"
    )
    assert browser.get(url).status_code == 404
    assert (
        browser.post(
            url, {"publication_digest": publication.publication_digest}
        ).status_code
        == 404
    )


def test_a_colleagues_receipt_does_not_clear_this_persons_next_action() -> None:
    # Given another person in this company has confirmed the exact publication.
    setup = publication_graph("publication-person")
    colleague = add_member(
        setup.graph.tenant,
        "colleague@publication-person.test",
        TenantRole.CLIENT_COLLABORATOR,
        client=setup.graph.client,
    )
    with tenant_context(setup.graph.tenant.pk):
        execute(setup.graph.actor, setup.proposal.pk, expected_version=2)
        publication = ClientPublication.objects.get(proposal=setup.proposal)
        ClientAcknowledgement.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            publication=publication,
            user=colleague,
            publication_digest=publication.publication_digest,
        )

    # When the unconfirmed person opens Inicio.
    response = setup.session().get("/")

    # Then their own receipt remains a real action.
    assert response.status_code == 200
    assert (
        Markup(response.content).one("data-primary-action", "publication")["href"]
        == f"/publicacoes/{publication.pk}/"
    )


def test_pending_publication_is_not_hidden_behind_twelve_confirmed_items() -> None:
    # Given one pending item older than the bounded publication history.
    setup = publication_graph("publication-history")
    with tenant_context(setup.graph.tenant.pk):
        execute(setup.graph.actor, setup.proposal.pk, expected_version=2)
        publication = ClientPublication.objects.get(proposal=setup.proposal)
        for occurrence in range(2, 14):
            setup.cycle.refresh_from_db()
            proposal = propose(
                setup.graph.staff,
                setup.cycle.pk,
                ProposalDraft(
                    ProposalInput(
                        action_kind=ActionKind.CLIENT_PUBLICATION,
                        step_key="publication",
                        occurrence=occurrence,
                        recipient_ids=(setup.contact.pk,),
                        subject=f"Confirmed history {occurrence}",
                        body="History",
                    ),
                    timezone.now() + timedelta(days=1),
                ),
                expected_version=setup.cycle.version,
            )
            proposal = approve(
                setup.graph.actor, proposal.pk, expected_version=1
            ).proposal
            execute(setup.graph.actor, proposal.pk, expected_version=2)
            confirmed = ClientPublication.objects.get(proposal=proposal)
            ClientAcknowledgement.objects.create(
                tenant=setup.graph.tenant,
                client=setup.graph.client,
                publication=confirmed,
                user=setup.portal_user,
                publication_digest=confirmed.publication_digest,
            )

    # When the recipient opens the bounded home.
    response = setup.session().get("/")

    # Then the older outstanding action is still offered.
    assert response.status_code == 200
    assert (
        Markup(response.content).one("data-primary-action", "publication")["href"]
        == f"/publicacoes/{publication.pk}/"
    )
