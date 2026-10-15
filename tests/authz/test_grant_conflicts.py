"""Two roster administrators cannot replay an instruction over a newer decision."""

from collections.abc import Mapping
from http import HTTPStatus

import pytest
from django.contrib.messages import get_messages
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.audit.models import Event
from apps.authz.models import Capability, GrantLevel, UserCapabilityGrant
from apps.authz.services import resolve_level
from apps.core.tenancy import tenant_context
from apps.tenants.models import Membership
from tests.authz.test_user_capability_grant import (
    REVIEWER_CAPABILITY,
    Cast,
    _membership,
    _toggle_url,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(name="cast")
def _cast() -> Cast:
    return Cast()


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]


def _state(cast: Cast) -> tuple[list[Mapping[str, object]], list[Mapping[str, object]]]:
    with tenant_context(cast.tenant.pk):
        return (
            list(UserCapabilityGrant.objects.order_by("pk").values()),
            list(Event.objects.order_by("pk").values()),
        )


def test_two_callers_cannot_replay_an_enable_after_a_newer_revoke(cast: Cast) -> None:
    membership = _membership(cast, cast.accountant)
    owner = cast.firm.as_owner()
    ops = cast.firm.sign_in(cast.ops)
    url = _toggle_url(membership)
    old_enable = {"enabled": "1", "expected_version": "0"}

    assert owner.post(url, old_enable).status_code == HTTPStatus.FOUND
    assert (
        ops.post(url, {"enabled": "0", "expected_version": "1"}).status_code
        == HTTPStatus.FOUND
    )
    before = _state(cast)

    response = owner.post(url, old_enable)

    assert response.status_code == HTTPStatus.CONFLICT
    assert _state(cast) == before
    assert any(
        message.level_tag == "error" for message in get_messages(response.wsgi_request)
    )
    assert 'name="expected_version" value="2"' in response.content.decode()
    with tenant_context(cast.tenant.pk):
        assert resolve_level(cast.accountant, REVIEWER_CAPABILITY) == GrantLevel.NONE
    membership.refresh_from_db()
    assert membership.review_grant_version == 2


@pytest.mark.parametrize("enabled", [False, True])
def test_service_reports_a_typed_conflict_without_any_writes(
    cast: Cast, enabled: bool
) -> None:
    from apps.authz.grants import (  # noqa: PLC0415
        GrantConflictError,
        set_review_capability,
    )

    membership = _membership(cast, cast.accountant)
    with tenant_context(cast.tenant.pk):
        set_review_capability(
            actor=cast.firm.owner,
            membership=membership,
            enabled=True,
            expected_version=0,
        )
        before = _state(cast)
        with pytest.raises(GrantConflictError):
            set_review_capability(
                actor=cast.ops,
                membership=membership,
                enabled=enabled,
                expected_version=0,
            )
        assert _state(cast) == before
    membership.refresh_from_db()
    assert membership.review_grant_version == 1


def test_current_version_noop_keeps_the_revision_and_audit_unchanged(
    cast: Cast,
) -> None:
    http = cast.firm.as_owner()
    membership = _membership(cast, cast.accountant)
    before = _state(cast)
    response = http.post(
        _toggle_url(membership), {"enabled": "0", "expected_version": "0"}
    )
    assert response.status_code == HTTPStatus.FOUND
    assert _state(cast) == before
    membership.refresh_from_db()
    assert membership.review_grant_version == 0


@pytest.mark.parametrize("version", [None, "", "-1", "1.0", "wrong", "9" * 100])
def test_malformed_version_is_refused_without_grant_writes(
    cast: Cast, version: str | None
) -> None:
    http = cast.firm.as_owner()
    data = {"enabled": "1"}
    if version is not None:
        data["expected_version"] = version
    before = _state(cast)
    response = http.post(_toggle_url(_membership(cast, cast.accountant)), data)
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert _state(cast) == before


def test_team_form_carries_the_revision_for_its_displayed_state(cast: Cast) -> None:
    response = cast.firm.as_owner().get(reverse("team"))
    assert response.status_code == HTTPStatus.OK
    assert 'name="expected_version" value="0"' in response.content.decode()


def test_roster_state_is_scoped_and_read_in_one_snapshot(cast: Cast) -> None:
    from apps.authz.grants import with_reviewer_state  # noqa: PLC0415

    with tenant_context(cast.tenant.pk):
        grant = UserCapabilityGrant.objects.create(
            tenant=cast.tenant,
            user=cast.accountant,
            capability=Capability.objects.get(slug=REVIEWER_CAPABILITY),
            level=GrantLevel.FULL,
        )
        UserCapabilityGrant.objects.create(
            tenant=cast.tenant,
            user=cast.colleague,
            capability=Capability.objects.get(slug="clients.view_all"),
            level=GrantLevel.FULL,
        )
        members = Membership.objects.for_user(cast.firm.owner).filter(
            tenant=cast.tenant, client__isnull=True
        )
        with CaptureQueriesContext(connection) as queries:
            state = {
                user_id: (active, version)
                for user_id, active, version in with_reviewer_state(
                    members
                ).values_list("user_id", "reviewer_active", "review_grant_version")
            }
        assert len(queries) == 1
        assert state[cast.accountant.pk] == (True, 0)
        assert state[cast.colleague.pk] == (False, 0)
        grant.revoked_at = timezone.now()
        grant.save(update_fields=["revoked_at"])
        assert not any(
            with_reviewer_state(members).values_list("reviewer_active", flat=True)
        )
