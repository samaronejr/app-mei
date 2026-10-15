"""Publication base tables and INSERT-only receipts enforce both scope dimensions."""

import pytest
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.http import Http404
from psycopg import sql
from psycopg.errors import ForeignKeyViolation
from pytest_django.fixtures import SettingsWrapper

from apps.clients.models import ClientAssignment, ClientCompany
from apps.core.tenancy import tenant_context
from apps.cycles.effects.executors import execute
from apps.cycles.models import (
    ActionExecution,
    ClientAcknowledgement,
    ClientPublication,
)
from apps.tenants.models import TenantRole
from tests.cycles.publication_fixtures import publication_graph
from tests.ui.factories import add_member

pytestmark = pytest.mark.django_db(transaction=True)


def test_publication_portal_scope_and_private_tables_have_positive_controls() -> None:

    setup = publication_graph()
    with tenant_context(setup.graph.tenant.pk):
        execute(setup.graph.actor, setup.proposal.pk, expected_version=2)
        publication = ClientPublication.objects.get(proposal=setup.proposal)
    with tenant_context(setup.graph.tenant.pk), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE app_portal")
        for client_id in ["", str(setup.graph.engagement.pk)]:
            cursor.execute("SELECT set_config('app.client_id', %s, true)", [client_id])
            cursor.execute("SELECT id FROM cycles_clientpublication")
            assert cursor.fetchall() == []
        cursor.execute(
            "SELECT set_config('app.client_id', %s, true)",
            [str(setup.graph.client.pk)],
        )
        cursor.execute("SELECT id FROM cycles_clientpublication")
        assert cursor.fetchall() == [(publication.pk,)]
        for statement in [
            "SELECT * FROM cycles_actionexecution",
            "SELECT * FROM cycles_clientacknowledgement",
            "SELECT * FROM cycles_actionproposal",
            "SELECT * FROM cycles_approval",
            "SELECT * FROM delivery_outboundmessage",
            "UPDATE cycles_clientpublication SET state='retirada'",
            "DELETE FROM cycles_clientpublication",
        ]:
            with pytest.raises(DatabaseError), transaction.atomic():
                cursor.execute(statement)
    with connection.cursor() as cursor:
        cursor.execute("SELECT id FROM cycles_clientpublication")
        assert cursor.fetchall() == []


def test_receipt_scope_digest_and_append_only_are_enforced_by_postgres() -> None:

    setup = publication_graph()
    with tenant_context(setup.graph.tenant.pk):
        execute(setup.graph.actor, setup.proposal.pk, expected_version=2)
        publication = ClientPublication.objects.get(proposal=setup.proposal)
        with pytest.raises(IntegrityError) as wrong_digest, transaction.atomic():
            ClientAcknowledgement.objects.create(
                tenant=setup.graph.tenant,
                client=setup.graph.client,
                publication=publication,
                user=setup.portal_user,
                publication_digest="f" * 64,
            )
        assert isinstance(wrong_digest.value.__cause__, ForeignKeyViolation)
        receipt = ClientAcknowledgement.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            publication=publication,
            user=setup.portal_user,
            publication_digest=publication.publication_digest,
        )
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user")
            assert cursor.fetchone() == ("app_runtime",)
            for statement in [
                (
                    "UPDATE cycles_clientacknowledgement "
                    "SET publication_digest=repeat('0',64)"
                ),
                "DELETE FROM cycles_clientacknowledgement",
                "TRUNCATE cycles_clientacknowledgement",
                "UPDATE cycles_actionexecution SET digest=repeat('0',64)",
                "DELETE FROM cycles_actionexecution",
                "TRUNCATE cycles_actionexecution",
            ]:
                with pytest.raises(DatabaseError), transaction.atomic():
                    cursor.execute(statement)
        receipt.refresh_from_db()
        assert receipt.publication_digest == publication.publication_digest


def test_foreign_tenant_publication_and_unassigned_staff_cannot_write(
    settings: SettingsWrapper,
) -> None:

    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    setup = publication_graph("pub-own")
    foreign = publication_graph("pub-foreign")
    with tenant_context(foreign.graph.tenant.pk):
        execute(foreign.graph.actor, foreign.proposal.pk, expected_version=2)
        publication = ClientPublication.objects.get(proposal=foreign.proposal)
    browser = setup.session()
    assert browser.get(f"/publicacoes/{publication.pk}/").status_code == 404
    response = browser.post(
        f"/publicacoes/{publication.pk}/",
        {"publication_digest": publication.publication_digest},
    )
    assert response.status_code == 404
    with tenant_context(setup.graph.tenant.pk):
        assert not ClientAcknowledgement.objects.exists()
        ClientAssignment.objects.filter(
            client=setup.graph.client, user=setup.graph.staff
        ).delete()
        with pytest.raises(Http404):
            execute(setup.graph.staff, setup.proposal.pk, expected_version=2)
        assert not ActionExecution.objects.exists()
        assert not ClientPublication.objects.exists()


def test_same_firm_other_client_cannot_view_or_acknowledge_publication(
    settings: SettingsWrapper,
) -> None:

    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    setup = publication_graph()
    with tenant_context(setup.graph.tenant.pk):
        execute(setup.graph.actor, setup.proposal.pk, expected_version=2)
        publication = ClientPublication.objects.get(proposal=setup.proposal)
        other_client = ClientCompany.objects.create(
            tenant=setup.graph.tenant,
            cnpj="11444777000161",
            legal_name="Outro cliente do mesmo escritório",
        )
    other_user = add_member(
        setup.graph.tenant,
        "other-client@publication.test",
        TenantRole.CLIENT_OWNER,
        client=other_client,
    )
    url = f"/publicacoes/{publication.pk}/"
    assert setup.session().get(url).status_code == 200
    foreign = setup.session(user=other_user)
    assert foreign.get(url).status_code == 404
    response = foreign.post(url, {"publication_digest": publication.publication_digest})
    assert response.status_code == 404
    with tenant_context(setup.graph.tenant.pk):
        assert not ClientAcknowledgement.objects.exists()


def test_receipt_and_execution_triggers_refuse_mutation_even_by_table_owner() -> None:
    setup = publication_graph()
    with tenant_context(setup.graph.tenant.pk):
        execute(setup.graph.actor, setup.proposal.pk, expected_version=2)
        publication = ClientPublication.objects.get(proposal=setup.proposal)
        ClientAcknowledgement.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            publication=publication,
            user=setup.portal_user,
            publication_digest=publication.publication_digest,
        )
        assert ClientAcknowledgement.objects.count() == 1
        assert ActionExecution.objects.count() == 1
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL ROLE app_test")
            for table in ("cycles_clientacknowledgement", "cycles_actionexecution"):
                for statement in (
                    sql.SQL("UPDATE {} SET id=id").format(sql.Identifier(table)),
                    sql.SQL("DELETE FROM {}").format(sql.Identifier(table)),
                ):
                    with (
                        pytest.raises(DatabaseError, match="append-only"),
                        transaction.atomic(),
                    ):
                        cursor.execute(statement)
