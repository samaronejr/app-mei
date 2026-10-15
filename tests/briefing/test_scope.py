"""A zero count is trustworthy only beside a positive unscoped control."""

import pytest

from apps.briefing.queries import briefing
from apps.briefing.types import BriefingFilters
from apps.clients.models import AssignmentRole, ClientAssignment
from apps.core.tenancy import tenant_context
from apps.obligations.models import Obligation
from tests.briefing.fixtures import briefing_setup, due_obligation
from tests.briefing.provider_fixtures import provider_graph, second_client
from tests.isolation.rolecheck import assert_isolated_role

pytestmark = pytest.mark.django_db(transaction=True)


def test_unassigned_staff_sees_neither_items_nor_counts_with_vacuity_control() -> None:
    setup = briefing_setup()
    due_obligation(setup)
    assert_isolated_role()
    with tenant_context(setup.tenant.pk):
        assert Obligation.objects.count() == 1
        control = briefing(setup.actor, scope="team", filters=BriefingFilters())
        assert control.total == 1
        assert len(control.items) == 1
        assert sum(count.total for count in control.counts) == 1

        page = briefing(setup.staff, scope="team", filters=BriefingFilters())

    assert page.items == ()
    assert page.total == 0
    assert page.counts
    assert all(count.total == 0 for count in page.counts)
    assert page.changes == ()


def test_an_assignment_revoked_between_reads_removes_items_and_counts() -> None:
    setup = briefing_setup()
    due_obligation(setup)
    with tenant_context(setup.tenant.pk):
        assignment = ClientAssignment.objects.create(
            tenant=setup.tenant,
            client=setup.client,
            user=setup.staff,
            role=AssignmentRole.PRIMARY,
        )
        before = briefing(setup.staff, scope="team", filters=BriefingFilters())
        assert before.total == 1
        assignment.delete()

        after = briefing(setup.staff, scope="team", filters=BriefingFilters())

    assert after.items == ()
    assert after.total == 0
    assert sum(count.total for count in after.counts) == 0


def test_cross_tenant_sources_never_enter_the_same_scoped_count() -> None:
    alpha = briefing_setup("briefing-alpha")
    beta = briefing_setup("briefing-beta")
    own = due_obligation(alpha)
    foreign = due_obligation(beta)
    assert_isolated_role()
    with tenant_context(beta.tenant.pk):
        control = briefing(beta.actor, scope="team", filters=BriefingFilters())
        assert {item.subject_ref for item in control.items} == {foreign.pk}
    with tenant_context(alpha.tenant.pk):
        page = briefing(alpha.actor, scope="team", filters=BriefingFilters())

    assert {item.subject_ref for item in page.items} == {own.pk}
    assert page.total == 1
    assert sum(count.total for count in page.counts) == 1


def test_every_provider_is_nonempty_before_unassigned_staff_is_denied() -> None:
    setup = briefing_setup()
    graph = provider_graph(setup)
    with tenant_context(setup.tenant.pk):
        control = briefing(setup.actor, scope="team", filters=BriefingFilters())
        assert {item.kind for item in control.items} == set(graph.subjects)
        assert all(count.total > 0 for count in control.counts)

        page = briefing(setup.staff, scope="team", filters=BriefingFilters())

    assert page.items == ()
    assert page.total == 0
    assert all(count.total == 0 for count in page.counts)


def test_each_provider_excludes_an_unassigned_client_in_the_same_tenant() -> None:
    setup = briefing_setup()
    own = provider_graph(setup)
    other = provider_graph(second_client(setup))
    with tenant_context(setup.tenant.pk):
        ClientAssignment.objects.create(
            tenant=setup.tenant, client=setup.client, user=setup.staff
        )
        control = briefing(setup.actor, scope="team", filters=BriefingFilters())
        assert all(count.total >= 2 for count in control.counts)
        assert set(other.subjects.values()) <= {
            item.subject_ref for item in control.items
        }

        page = briefing(setup.staff, scope="team", filters=BriefingFilters())

    assert {item.kind for item in page.items} == set(own.subjects)
    assert {item.client_id for item in page.items} == {setup.client.pk}
    assert all(count.total == 1 for count in page.counts)
    assert sum(count.total for count in page.counts) == page.total == 7
