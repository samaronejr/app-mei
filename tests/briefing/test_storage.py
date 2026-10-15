"""The briefing persists only private, tenant- and client-paired records."""

import pytest
from django.db import IntegrityError, connection, transaction

from apps.core.tenancy import tenant_context
from apps.cycles.models import BriefingFeedback, SavedView
from tests.briefing.fixtures import briefing_setup, due_obligation
from tests.isolation.rolecheck import assert_isolated_role

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("table", ["cycles_savedview", "cycles_briefingfeedback"])
def test_portal_has_no_privileges_on_the_new_private_tables(table: str) -> None:
    with connection.cursor() as cursor:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            cursor.execute(
                "SELECT has_table_privilege('app_portal', %s, %s)",
                [table, privilege],
            )
            assert cursor.fetchone() == (False,)


def test_saved_views_are_scoped_by_the_runtime_tenant() -> None:
    alpha = briefing_setup("view-storage-alpha")
    beta = briefing_setup("view-storage-beta")
    assert_isolated_role()
    with tenant_context(alpha.tenant.pk):
        own = SavedView.objects.create(
            tenant=alpha.tenant, user=alpha.actor, name="Minha carteira"
        )
    with tenant_context(beta.tenant.pk):
        foreign = SavedView.objects.create(
            tenant=beta.tenant, user=beta.actor, name="Minha carteira"
        )
        assert list(SavedView.objects.values_list("pk", flat=True)) == [foreign.pk]
    with tenant_context(alpha.tenant.pk):
        assert list(SavedView.objects.values_list("pk", flat=True)) == [own.pk]
        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM cycles_savedview")
            assert cursor.fetchall() == [(own.pk,)]


def test_feedback_cannot_pair_a_tenant_with_another_firms_client() -> None:
    alpha = briefing_setup("feedback-storage-alpha")
    beta = briefing_setup("feedback-storage-beta")
    subject = due_obligation(beta)
    with (
        tenant_context(alpha.tenant.pk),
        pytest.raises(IntegrityError),
        transaction.atomic(),
    ):
        BriefingFeedback.objects.create(
            tenant=alpha.tenant,
            client=beta.client,
            user=alpha.actor,
            kind="obligation",
            subject_ref=subject.pk,
            fact_version="1",
            reason="Não faz parte desta carteira",
        )
