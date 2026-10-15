"""Saved filters never preserve an assignment or bypass current authorization."""

import pytest
from django.core.exceptions import ValidationError
from django.http import Http404

from apps.audit.models import Event
from apps.briefing.queries import briefing
from apps.briefing.saved_views import delete_view, resolve_view, save_view
from apps.clients.models import ClientAssignment
from apps.core.tenancy import tenant_context
from apps.cycles.errors import VersionConflict
from apps.cycles.models import SavedView
from tests.briefing.fixtures import briefing_setup, due_obligation

pytestmark = pytest.mark.django_db(transaction=True)


def test_saved_client_filter_is_reauthorized_after_unassignment() -> None:
    setup = briefing_setup()
    due_obligation(setup)
    with tenant_context(setup.tenant.pk):
        assignment = ClientAssignment.objects.create(
            tenant=setup.tenant, client=setup.client, user=setup.staff
        )
        saved = save_view(
            setup.staff,
            "Meu cliente",
            {"cliente": str(setup.client.pk)},
            expected_version=0,
        )
        scope, filters = resolve_view(setup.staff, saved.pk)
        assert briefing(setup.staff, scope=scope, filters=filters).total == 1
        assignment.delete()

        scope, filters = resolve_view(setup.staff, saved.pk)
        page = briefing(setup.staff, scope=scope, filters=filters)

    assert page.items == ()
    assert page.total == 0
    assert all(count.total == 0 for count in page.counts)


def test_another_users_saved_view_is_not_a_read_or_delete_capability() -> None:
    setup = briefing_setup()
    with tenant_context(setup.tenant.pk):
        saved = save_view(setup.actor, "Equipe", {}, expected_version=0)
        with pytest.raises(Http404):
            resolve_view(setup.staff, saved.pk)
        with pytest.raises(Http404):
            delete_view(setup.staff, saved.pk, expected_version=saved.version)
        assert SavedView.objects.filter(pk=saved.pk).exists()


def test_a_stale_save_cannot_overwrite_the_newer_filters_or_audit() -> None:
    setup = briefing_setup()
    with tenant_context(setup.tenant.pk):
        first = save_view(setup.actor, "Equipe", {}, expected_version=0)
        second = save_view(
            setup.actor, "Equipe", {"uf": "SP"}, expected_version=first.version
        )
        assert second.version == 2
        before = Event.objects.filter(object_type="SavedView").count()

        with pytest.raises(VersionConflict):
            save_view(
                setup.actor, "Equipe", {"uf": "RJ"}, expected_version=first.version
            )

        stored = SavedView.objects.get(pk=first.pk)
        assert stored.filters_json["uf"] == "SP"
        assert stored.version == 2
        assert Event.objects.filter(object_type="SavedView").count() == before


def test_stale_delete_refuses_to_remove_a_changed_saved_view() -> None:
    setup = briefing_setup()
    with tenant_context(setup.tenant.pk):
        first = save_view(setup.actor, "Equipe", {}, expected_version=0)
        second = save_view(
            setup.actor, "Equipe", {"uf": "SP"}, expected_version=first.version
        )
        with pytest.raises(VersionConflict):
            delete_view(setup.actor, first.pk, expected_version=first.version)
        assert SavedView.objects.filter(pk=second.pk).exists()


@pytest.mark.parametrize(
    "filters",
    [{"responsavel": "not-a-uuid"}, {"papel": "owner"}, {"escopo": "all"}],
)
def test_invalid_saved_filters_write_no_preference_or_event(
    filters: dict[str, str],
) -> None:
    setup = briefing_setup()
    with tenant_context(setup.tenant.pk):
        with pytest.raises(ValidationError):
            save_view(setup.actor, "Inválida", filters, expected_version=0)
        assert not SavedView.objects.exists()
        assert not Event.objects.filter(object_type="SavedView").exists()
