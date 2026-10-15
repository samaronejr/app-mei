"""Portal SELECT is client-restrictive and private cycle tables remain unreadable."""

import pytest
from django.db import DatabaseError, connection, transaction
from django.test import Client
from django.test.utils import override_settings
from psycopg import sql

from apps.core.tenancy import tenant_context
from apps.cycles.models import ServiceCycle
from apps.cycles.services.cycle import open_cycle
from apps.cycles.workspace import portal_projection
from apps.tenants.models import Membership, TenantRole
from tests.browser.harness import mfa_session_cookie
from tests.conftest import PORTAL_DENIED_TABLES
from tests.cycles.cycle_fixtures import PERIOD, cycle_setup
from tests.support import enrol_totp

pytestmark = pytest.mark.django_db(transaction=True)


def test_portal_cycle_base_table_isolation_and_vacuity_control() -> None:
    setup = cycle_setup()
    with tenant_context(setup.tenant.pk):
        ref = open_cycle(
            setup.actor,
            setup.engagement.pk,
            PERIOD,
            setup.definition.pk,
            idempotency_key="portal",
        )
    other_client_id = setup.engagement.pk  # a valid UUID, never this client's identity
    with (
        tenant_context(setup.tenant.pk),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute("SET LOCAL ROLE app_portal")
        cursor.execute(
            "SELECT set_config('app.client_id', %s, true)", [str(other_client_id)]
        )
        cursor.execute("SELECT id FROM cycles_servicecycle")
        assert cursor.fetchall() == []
        cursor.execute(
            "SELECT set_config('app.client_id', %s, true)", [str(setup.client.pk)]
        )
        cursor.execute("SELECT id FROM cycles_servicecycle")
        assert cursor.fetchall() == [(ref.id,)]
        for table in PORTAL_DENIED_TABLES:
            with pytest.raises(DatabaseError), transaction.atomic():
                cursor.execute(
                    sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))
                )
        for operation in [
            "UPDATE cycles_servicecycle SET version=99",
            "DELETE FROM cycles_servicecycle",
        ]:
            with pytest.raises(DatabaseError), transaction.atomic():
                cursor.execute(operation)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE app_test")
        cursor.execute("SELECT id FROM cycles_servicecycle")
        assert cursor.fetchall() == [(ref.id,)]
        cursor.execute(
            "SELECT count(*) FROM pg_views WHERE schemaname='public' "
            "AND viewname LIKE 'cycles_%'"
        )
        assert cursor.fetchone() == (0,)


@override_settings(ALLOWED_HOSTS=[".localhost", "localhost", "testserver"])
def test_portal_foreign_cycle_is_404_and_projection_has_only_three_fields() -> None:
    setup = cycle_setup()
    # A second real client in the same firm, with its own cycle.
    from apps.clients.models import ClientCompany, Engagement  # noqa: PLC0415

    with tenant_context(setup.tenant.pk):
        ref = open_cycle(
            setup.actor,
            setup.engagement.pk,
            PERIOD,
            setup.definition.pk,
            idempotency_key="a",
        )
        other = ClientCompany.objects.create(
            tenant=setup.tenant,
            legal_name="Other client",
            cnpj="11444777000161",
            status="active",
        )
        engagement = Engagement.objects.create(
            tenant=setup.tenant,
            client=other,
            service_kind="mei_mensal",
            starts_on=PERIOD,
            provenance="staff_entry",
        )
        # Seed a sibling case without needing another fiscal profile; paired keys
        # still constrain this row at the database boundary.
        foreign = ServiceCycle.objects.create(
            tenant=setup.tenant,
            client=other,
            engagement=engagement,
            playbook_id=setup.definition.playbook_id,
            playbook_version=setup.definition,
            period_start=PERIOD,
            period_end=PERIOD,
            state="planejado",
        )
        assert set(portal_projection(ServiceCycle.objects.get(pk=ref.id))) == {
            "period",
            "state_label",
            "next_action",
        }
    Membership.objects.filter(user=setup.staff).delete()
    Membership.objects.create(
        tenant=setup.tenant,
        user=setup.staff,
        client=setup.client,
        role=TenantRole.CLIENT_OWNER,
    )
    session = Client(SERVER_NAME=f"{setup.tenant.slug}-portal.localhost")
    enrol_totp(setup.staff)
    cookie = mfa_session_cookie(
        setup.staff, f"http://{setup.tenant.slug}-portal.localhost/"
    )
    session.cookies[cookie["name"]] = cookie["value"]
    own = session.get(f"/ciclos/{ref.id}/")
    assert own.status_code == 200
    assert session.get(f"/ciclos/{foreign.pk}/").status_code == 404
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE app_test")
        cursor.execute("SELECT id FROM cycles_servicecycle WHERE id=%s", [foreign.pk])
        assert cursor.fetchone() == (foreign.pk,)
