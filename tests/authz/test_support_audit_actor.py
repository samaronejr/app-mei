"""Support audit actor identity survives real logout and request failures."""

from http import HTTPStatus

import pytest
from django.contrib.auth import SESSION_KEY
from django.contrib.auth.models import AnonymousUser
from django.http import HttpRequest, HttpResponse
from django.urls import reverse
from django.utils.functional import SimpleLazyObject

from apps.accounts.adapter import InvitationOnlyAccountAdapter
from apps.accounts.models import User
from apps.audit.middleware import SupportRequestAuditMiddleware
from apps.audit.models import AuditAction, Event, PlatformEvent
from apps.audit.services import ObjectRef, record_event
from apps.authz.support import current_support
from apps.core.tenancy import current_tenant_id, tenant_context
from apps.tenants.middleware import TenantHttpRequest
from apps.tenants.support_models import SupportGrant
from tests.authz.test_support_grant import (
    _firm_host,
    _grant_for,
    _session_for,
    _staff,
)
from tests.tenants.test_cancellation_retention import enabled_firm

pytestmark = pytest.mark.django_db(transaction=True)


def _assert_request(
    grant: SupportGrant, actor: User, metadata: dict[str, object]
) -> tuple[PlatformEvent, Event]:
    platform = PlatformEvent.objects.get(
        action=AuditAction.SUPPORT_REQUEST,
        metadata__grant_id=str(grant.pk),
        metadata__method=metadata["method"],
        metadata__path=metadata["path"],
    )
    with tenant_context(grant.tenant_id):
        event = Event.objects.get(
            action=AuditAction.SUPPORT_REQUEST,
            object_type="SupportGrant",
            object_id=str(grant.pk),
            metadata__method=metadata["method"],
            metadata__path=metadata["path"],
        )
    assert platform.actor_id == event.actor_id == actor.pk
    assert platform.tenant_id == event.tenant_id == grant.tenant_id
    assert platform.metadata == {"grant_id": str(grant.pk), **metadata}
    assert event.metadata == metadata
    return platform, event


def _assert_context_cleared() -> None:
    assert current_tenant_id.get() is None
    assert current_support.get() is None


@pytest.mark.parametrize(
    ("method", "route", "status"),
    [
        ("GET", "dashboard", HTTPStatus.OK),
        ("POST", "account_logout", HTTPStatus.FOUND),
    ],
)
def test_authenticated_get_and_real_logout_keep_both_audit_actors(
    method: str, route: str, status: HTTPStatus
) -> None:
    # Given a live owner-approved grant and the ordinary authenticated HTTP stack.
    firm = enabled_firm("support-actor-handler")
    staff = _staff()
    grant = _grant_for(firm.tenant, firm.owner, staff)
    session = _session_for(staff, _firm_host(firm))
    path = reverse(route)

    # When the registered handler executes, including allauth's real POST logout.
    if method == "POST":
        response = session.post(
            f"{path}?private=QUERY_CANARY", {"private": "POST_CANARY"}
        )
    else:
        response = session.get(f"{path}?private=QUERY_CANARY")

    # Then logout really replaced the user, but both append-only rows keep the actor.
    assert response.status_code == status
    assert response.wsgi_request.user.is_authenticated == (method == "GET")
    assert (SESSION_KEY in response.wsgi_request.session) == (method == "GET")
    _assert_request(
        grant, staff, {"method": method, "path": path, "status_code": status}
    )
    assert PlatformEvent.objects.filter(action=AuditAction.SUPPORT_REQUEST).count() == 1
    with tenant_context(firm.tenant.pk):
        assert Event.objects.filter(action=AuditAction.SUPPORT_REQUEST).count() == 1
    _assert_context_cleared()

    # The actual owner trail projects the matching row's account, not the grant header.
    owner = _session_for(firm.owner, _firm_host(firm))
    trail = owner.get(f"/configuracoes/suporte/{grant.pk}/trilha/")
    assert trail.status_code == HTTPStatus.OK
    matching_rows = [
        row
        for row in trail.context["rows"]
        if row["method"] == method and row["path"] == path
    ]
    assert len(matching_rows) == 1
    assert matching_rows[0]["actor"] == staff.email
    _assert_context_cleared()


@pytest.mark.parametrize("raise_after_replacement", [False, True])
def test_lazy_actor_is_resolved_before_the_inner_handler(
    raise_after_replacement: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a real approved grant and a lazy identity that changes at handler entry.
    firm = enabled_firm("support-actor-lazy")
    staff = _staff()
    grant = _grant_for(firm.tenant, firm.owner, staff)
    request = TenantHttpRequest()
    request.path = request.path_info = reverse("account_logout")
    request.method = "POST"
    request.support_grant = grant
    handler_started = False
    resolution_phases: list[bool] = []

    def resolve_actor() -> User | AnonymousUser:
        resolution_phases.append(handler_started)
        return AnonymousUser() if handler_started else staff

    monkeypatch.setattr(request, "user", SimpleLazyObject(resolve_actor), raising=False)

    def replace_identity(inner: HttpRequest) -> HttpResponse:
        nonlocal handler_started
        handler_started = True
        inner.user = AnonymousUser()
        if raise_after_replacement:
            raise RuntimeError
        return HttpResponse(status=HTTPStatus.FORBIDDEN)

    # When the inner handler replaces identity without first evaluating the wrapper.
    middleware = SupportRequestAuditMiddleware(replace_identity)
    if raise_after_replacement:
        with pytest.raises(RuntimeError):
            middleware(request)
        status = HTTPStatus.INTERNAL_SERVER_ERROR
    else:
        response = middleware(request)
        status = HTTPStatus.FORBIDDEN
        assert response.status_code == status

    # Then identity was evaluated before entry, not first read after replacement.
    _assert_request(
        grant,
        staff,
        {
            "method": "POST",
            "path": request.path_info,
            "status_code": status,
        },
    )
    assert resolution_phases == [False]
    _assert_context_cleared()


def test_failure_after_real_logout_preserves_audits_outside_view_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given real logout, followed by a narrow injected failure inside its view.
    firm = enabled_firm("support-actor-failure")
    staff = _staff()
    grant = _grant_for(firm.tenant, firm.owner, staff)
    session = _session_for(staff, _firm_host(firm))
    session.raise_request_exception = False
    path = reverse("account_logout")
    original_logout = InvitationOnlyAccountAdapter.logout
    rolled_back_id = "t50-logout-rollback"

    def logout_then_fail(
        adapter: InvitationOnlyAccountAdapter, request: HttpRequest
    ) -> None:
        original_logout(adapter, request)
        assert not request.user.is_authenticated
        assert SESSION_KEY not in request.session
        record_event(
            action=AuditAction.EXPORT,
            actor=staff,
            obj=ObjectRef(type="T50Probe", id=rolled_back_id),
        )
        raise RuntimeError(rolled_back_id)

    monkeypatch.setattr(InvitationOnlyAccountAdapter, "logout", logout_then_fail)

    # When the real authorized POST fails only after Django has replaced request.user.
    response = session.post(path)

    # Then the view write rolled back, while both independent request audits survived.
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert not response.wsgi_request.user.is_authenticated
    with tenant_context(firm.tenant.pk):
        assert not Event.objects.filter(
            object_type="T50Probe", object_id=rolled_back_id
        ).exists()
    _assert_request(
        grant,
        staff,
        {
            "method": "POST",
            "path": path,
            "status_code": HTTPStatus.INTERNAL_SERVER_ERROR,
        },
    )
    _assert_context_cleared()


def test_denied_owner_route_keeps_known_support_actor() -> None:
    # Given a support account that may not read the owner's audit administration.
    firm = enabled_firm("support-actor-denied")
    staff = _staff()
    grant = _grant_for(firm.tenant, firm.owner, staff)
    session = _session_for(staff, _firm_host(firm))
    path = f"/configuracoes/suporte/{grant.pk}/trilha/"

    # When it tries the actual owner-only route.
    response = session.get(path)

    # Then authorization still denies, and both streams persist the known actor.
    assert response.status_code == HTTPStatus.FORBIDDEN
    _assert_request(
        grant,
        staff,
        {"method": "GET", "path": path, "status_code": HTTPStatus.FORBIDDEN},
    )
    _assert_context_cleared()


def test_actor_and_grant_do_not_leak_across_logout_and_account_switch() -> None:
    # Given two independently approved actors using the same Client middleware chain.
    firm = enabled_firm("support-actor-switch")
    first = _staff()
    second = _staff("support-second@platform.example.com")
    first_grant = _grant_for(firm.tenant, firm.owner, first)
    second_grant = _grant_for(firm.tenant, firm.owner, second)
    session = _session_for(first, _firm_host(firm))
    path = reverse("account_logout")

    # When the first actor logs out, an anonymous request intervenes, then B signs in.
    assert session.post(path).status_code == HTTPStatus.FOUND
    first_platform, first_event = _assert_request(
        first_grant,
        first,
        {"method": "POST", "path": path, "status_code": HTTPStatus.FOUND},
    )
    _assert_context_cleared()
    anonymous = session.get("/")
    assert not anonymous.wsgi_request.user.is_authenticated
    _assert_context_cleared()
    session.cookies = _session_for(second, _firm_host(firm)).cookies
    assert session.get("/").status_code == HTTPStatus.OK

    # Then B owns its own row; neither the anonymous request nor B rewrote A's row.
    _assert_request(
        second_grant,
        second,
        {"method": "GET", "path": "/", "status_code": HTTPStatus.OK},
    )
    saved_platform, saved_event = _assert_request(
        first_grant,
        first,
        {"method": "POST", "path": path, "status_code": HTTPStatus.FOUND},
    )
    assert (saved_platform.pk, saved_event.pk) == (first_platform.pk, first_event.pk)
    assert PlatformEvent.objects.filter(action=AuditAction.SUPPORT_REQUEST).count() == 2
    with tenant_context(firm.tenant.pk):
        assert Event.objects.filter(action=AuditAction.SUPPORT_REQUEST).count() == 2
    _assert_context_cleared()


def test_redacted_failed_path_keeps_actor_and_excludes_query() -> None:
    # Given a registered credential-bearing path and an authenticated live grant.
    firm = enabled_firm("support-actor-redaction")
    staff = _staff()
    grant = _grant_for(firm.tenant, firm.owner, staff)
    session = _session_for(staff, _firm_host(firm))
    path = reverse("invite-accept", args=["T50_CREDENTIAL_CANARY"])

    # When the ordinary registered handler rejects this credential.
    response = session.get(f"{path}?private=QUERY_CANARY")

    # Then both structured records keep identity but only a redacted path.
    assert response.status_code == HTTPStatus.NOT_FOUND
    _assert_request(
        grant,
        staff,
        {
            "method": "GET",
            "path": "/convites/aceitar/<redacted>/",
            "status_code": HTTPStatus.NOT_FOUND,
        },
    )
    _assert_context_cleared()


def test_infrastructure_exemption_does_not_evaluate_lazy_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a health request whose lazy authentication must never perform a lookup.
    request = HttpRequest()
    request.path = request.path_info = "/healthz"
    request.method = "GET"

    def forbidden_lookup() -> User:
        pytest.fail("infrastructure request evaluated authentication")

    monkeypatch.setattr(
        request, "user", SimpleLazyObject(forbidden_lookup), raising=False
    )

    # When the audit middleware serves an exempt request.
    response = SupportRequestAuditMiddleware(lambda _: HttpResponse())(request)

    # Then no authentication lookup or support audit write occurred.
    assert response.status_code == HTTPStatus.OK
    assert not PlatformEvent.objects.filter(action=AuditAction.SUPPORT_REQUEST).exists()
    assert current_tenant_id.get() is None
    assert current_support.get() is None
