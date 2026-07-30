"""`core.E010` refuses a portal view whose capability gate would refuse its holders.

`require_can` passes no object, and both object-refined levels answer False when the
object is None. So a portal view gated below FULL is an unconditional 403 — for the
roles that hold it there and no others, which is why it survives testing as an owner
and fails only for collaborators in production.

The pair below is the guard-family convention this project applies to every check that
can currently find nothing: silent on the real urlconf, and *provably able to report*
against a synthetic one. Without the second, the check would pass today because the
portal has no gated views yet, and would keep passing if it stopped working.
"""

from collections.abc import Callable

import pytest
from django.http import HttpRequest, HttpResponse
from django.urls import URLPattern, path

from apps.authz.services import PORTAL_CAPABILITY_GATES, require_can
from apps.core.checks import _portal_capability_gate_errors

FULL_FOR_BOTH_CLIENT_ROLES = "documents.transfer"
LIMITED_FOR_A_CLIENT_ROLE = "invoices.issue"
NOT_IN_THE_MATRIX = "documents.teleport"


@require_can(FULL_FOR_BOTH_CLIENT_ROLES)
def _vault_view(_request: HttpRequest) -> HttpResponse:
    return HttpResponse("ok")


@require_can(LIMITED_FOR_A_CLIENT_ROLE)
def _refusing_view(_request: HttpRequest) -> HttpResponse:
    return HttpResponse("ok")


@require_can(NOT_IN_THE_MATRIX)
def _unknown_view(_request: HttpRequest) -> HttpResponse:
    return HttpResponse("ok")


urlpatterns: list[URLPattern] = []


def _urlconf_with(*views: Callable[[HttpRequest], HttpResponse]) -> str:
    module = __name__
    urlpatterns.clear()
    urlpatterns.extend(path(f"v{i}", view) for i, view in enumerate(views))
    return module


def test_the_check_is_silent_on_the_real_portal_urlconf() -> None:
    # Given the portal urlconf as it actually ships
    # When the check runs
    errors = _portal_capability_gate_errors()

    # Then it reports nothing — the positive control that makes a report meaningful
    assert errors == []


def test_the_check_is_silent_on_a_gate_that_is_full_for_both_client_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a portal view gated exactly as constraint 5 requires
    monkeypatch.setattr(
        "apps.portal.middleware.PORTAL_URLCONF",
        _urlconf_with(_vault_view),
    )

    # When the check runs
    # Then it passes, so the report below is not simply "any gate is an error"
    assert _portal_capability_gate_errors() == []


def test_the_check_sees_a_gate_that_would_refuse_a_client_collaborator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a portal view gated on a capability held at LIMITED by one client role
    monkeypatch.setattr(
        "apps.portal.middleware.PORTAL_URLCONF",
        _urlconf_with(_refusing_view),
    )

    # When the check runs
    errors = _portal_capability_gate_errors()

    # Then it refuses to boot, and names the role the gate would silently 403
    assert len(errors) == 1
    assert errors[0].id == "core.E010"
    assert LIMITED_FOR_A_CLIENT_ROLE in str(errors[0].msg)
    assert "client_collaborator" in str(errors[0].msg)


def test_the_check_sees_a_gate_on_a_capability_that_does_not_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a portal view gated on a slug absent from the matrix, which resolve_level
    # would raise UnknownCapability for at request time rather than at boot
    monkeypatch.setattr(
        "apps.portal.middleware.PORTAL_URLCONF",
        _urlconf_with(_unknown_view),
    )

    # When the check runs
    errors = _portal_capability_gate_errors()

    # Then the typo is caught before the process serves anything
    assert len(errors) == 1
    assert NOT_IN_THE_MATRIX in str(errors[0].msg)


def test_require_can_registers_every_gate_it_creates() -> None:
    # Given the three views decorated above
    # Then each is registered with the capability it demands, which is what lets the
    # check enumerate by walking a urlconf instead of reading __closure__
    assert PORTAL_CAPABILITY_GATES[_vault_view] == FULL_FOR_BOTH_CLIENT_ROLES
    assert PORTAL_CAPABILITY_GATES[_refusing_view] == LIMITED_FOR_A_CLIENT_ROLE
    assert PORTAL_CAPABILITY_GATES[_unknown_view] == NOT_IN_THE_MATRIX
