"""Guard every dynamic URL path with a credential decision.

The registry is keyed by URL name for the same reason as
``is_public_post_endpoint``: that predicate resolves against ``request.urlconf``, and
there is no request at test time. Load the literal ``"config.urls"`` and
``"apps.portal.urls"`` trees separately instead. The portal URL module documents that
allauth is mounted at the identical path in both trees, so name matching remains the
shared control even if allauth later moves those paths.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.urls import URLPattern, URLResolver, get_resolver, include, path

URLCONFS: Final = ("config.urls", "apps.portal.urls")

EXPECTED_CREDENTIAL_NAMES: Final = frozenset(
    {
        "invite-accept",
        "portal-invite-accept",
        "account_confirm_email",
        "account_reset_password_from_key",
    },
)
EXPECTED_EXEMPTION_KEYS: Final = frozenset(
    {
        "member-deactivate",
        "member-reactivate",
        "invite-revoke",
        "portal-invite-issue",
        "client-detail",
        "portal-document-download",
        "admin:*",
    },
)

# This is deliberately narrower than an ``admin`` namespace skip. It covers the
# standard identifiers emitted by Django's own admin resolver, while a planted
# ``<str:token>`` inside that namespace must still be reported.
ADMIN_NAMESPACE_POLICY = "admin:*"
ADMIN_OBJECT_ID_PARAMETERS: Final = frozenset(
    {"app_label", "content_type_id", "object_id", "url"},
)


@dataclass(frozen=True, slots=True)
class DynamicRoute:
    """One named or unnamed URL pattern carrying dynamic path parameters."""

    urlconf: str
    name: str | None
    namespace: str
    parameters: frozenset[str]
    pattern: str

    @property
    def qualified_name(self) -> str | None:
        """Return the resolver-qualified URL name when the pattern has one."""
        if self.name is None:
            return None
        return ":".join(part for part in (self.namespace, self.name) if part)

    @property
    def label(self) -> str:
        """Return a stable, useful offender label even for unnamed patterns."""
        return self.qualified_name or f"{self.namespace or '<root>'}:<unnamed>"


def _walk_dynamic_routes(
    patterns: Iterable[URLPattern | URLResolver],
    *,
    urlconf: str,
    namespace: str = "",
    prefix: str = "",
    inherited_parameters: frozenset[str] = frozenset(),
) -> list[DynamicRoute]:
    """Return every leaf pattern carrying a dynamic parameter."""
    found: list[DynamicRoute] = []
    for candidate in patterns:
        pattern = str(candidate.pattern)
        parameters = inherited_parameters | frozenset(
            candidate.pattern.regex.groupindex,
        )
        if isinstance(candidate, URLPattern):
            if parameters:
                found.append(
                    DynamicRoute(
                        urlconf=urlconf,
                        name=candidate.name,
                        namespace=namespace,
                        parameters=parameters,
                        pattern=f"{prefix}{pattern}",
                    ),
                )
            continue

        child_namespace = ":".join(
            part for part in (namespace, candidate.namespace or "") if part
        )
        found.extend(
            _walk_dynamic_routes(
                candidate.url_patterns,
                urlconf=urlconf,
                namespace=child_namespace,
                prefix=f"{prefix}{pattern}",
                inherited_parameters=parameters,
            ),
        )
    return found


def _dynamic_routes(urlconf: str) -> list[DynamicRoute]:
    """Load one URL tree by name and require its dynamic-route scan to be real."""
    routes = _walk_dynamic_routes(
        get_resolver(urlconf).url_patterns,
        urlconf=urlconf,
    )
    assert routes, (
        f"{urlconf} exposed no dynamic-parameter route; the guard would pass "
        "vacuously for this URL tree"
    )
    return routes


def _has_written_exemption(
    route: DynamicRoute,
    exemptions: Mapping[str, str],
) -> bool:
    """Report whether a route has an explicit, non-empty exemption reason."""
    qualified_name = route.qualified_name
    if qualified_name is not None and exemptions.get(qualified_name, "").strip():
        return True
    return (
        route.namespace == "admin"
        and route.parameters <= ADMIN_OBJECT_ID_PARAMETERS
        and bool(exemptions.get(ADMIN_NAMESPACE_POLICY, "").strip())
    )


def _unregistered_routes(
    routes: Iterable[DynamicRoute],
    registry: Iterable[str],
    exemptions: Mapping[str, str],
) -> list[DynamicRoute]:
    """Return dynamic routes with neither registry membership nor a reason."""
    registered = frozenset(registry)
    return sorted(
        (
            route
            for route in routes
            if route.qualified_name not in registered
            and not _has_written_exemption(route, exemptions)
        ),
        key=lambda route: (route.urlconf, route.label, route.pattern),
    )


def _report(routes: Iterable[DynamicRoute]) -> str:
    """Name every uncovered route, its parameters, pattern and source tree."""
    lines = [
        f"{route.urlconf}: {route.label} {sorted(route.parameters)} {route.pattern}"
        for route in routes
    ]
    return "dynamic route has no credential decision:\n" + "\n".join(lines)


def _unused_view(
    _request: HttpRequest,
    **_parameters: str,
) -> HttpResponse:
    return HttpResponse()


def test_every_dynamic_route_is_registered_or_justifiably_exempted() -> None:
    """Every dynamic route in each production tree has an explicit decision."""
    # Kept as two calls so each URL tree must independently satisfy the non-vacuity
    # assertion inside ``_dynamic_routes``. Combining first would let one hide the
    # other's empty walk.
    routes_by_urlconf = {urlconf: _dynamic_routes(urlconf) for urlconf in URLCONFS}
    routes = [route for urlconf in URLCONFS for route in routes_by_urlconf[urlconf]]

    uncovered = _unregistered_routes(
        routes,
        settings.CREDENTIAL_BEARING_URL_NAMES,
        settings.CREDENTIAL_ROUTE_EXEMPTIONS,
    )
    assert uncovered == [], _report(uncovered)

    assert frozenset(settings.CREDENTIAL_BEARING_URL_NAMES) == (
        EXPECTED_CREDENTIAL_NAMES
    )
    assert frozenset(settings.CREDENTIAL_ROUTE_EXEMPTIONS) == (EXPECTED_EXEMPTION_KEYS)
    assert all(
        reason.strip() for reason in settings.CREDENTIAL_ROUTE_EXEMPTIONS.values()
    ), "every credential-route exemption must carry a written reason"


def test_the_guard_reports_a_planted_credential_route() -> None:
    # Given a key-shaped parameter rather than the rejected token-name-only heuristic
    planted = (
        path(
            "plantado/segredo/<str:key>/",
            _unused_view,
            name="planted-credential",
        ),
    )
    routes = _walk_dynamic_routes(planted, urlconf="<planted>")

    # When the fabricated route goes through the same predicate as the real guard
    reported = _unregistered_routes(routes, EXPECTED_CREDENTIAL_NAMES, {})

    # Then its dynamic credential cannot pass invisibly.
    assert [route.label for route in reported] == ["planted-credential"]


def test_the_admin_policy_reports_a_planted_credential_in_its_namespace() -> None:
    # Given a credential route nested inside a fabricated admin namespace
    sensitive_patterns = (
        path(
            "segredo/<str:token>/",
            _unused_view,
            name="planted-admin-credential",
        ),
    )
    planted = (
        path(
            "admin/",
            include((sensitive_patterns, "admin"), namespace="admin"),
        ),
    )
    routes = _walk_dynamic_routes(planted, urlconf="<planted>")
    admin_policy = {
        ADMIN_NAMESPACE_POLICY: settings.CREDENTIAL_ROUTE_EXEMPTIONS[
            ADMIN_NAMESPACE_POLICY
        ],
    }

    # When the same predicate applies the declared, narrowed namespace policy
    reported = _unregistered_routes(routes, (), admin_policy)

    # Then the policy does not become a blanket exclusion that conceals the secret.
    assert [route.label for route in reported] == [
        "admin:planted-admin-credential",
    ]


def test_a_planted_written_exemption_removes_only_its_route() -> None:
    # Given one fabricated object-id route and an explicit reason for that route alone
    planted = (
        path(
            "plantado/objetos/<uuid:pk>/",
            _unused_view,
            name="planted-object-detail",
        ),
    )
    routes = _walk_dynamic_routes(planted, urlconf="<planted>")
    no_exemptions = _unregistered_routes(routes, (), {})
    written_exemption = {
        "planted-object-detail": "A fabricated opaque object id, not a credential.",
    }

    # Then the control is reported without the reason and accepted with it, proving the
    # exemption path subtracts something rather than merely reading as rigour.
    assert [route.label for route in no_exemptions] == ["planted-object-detail"]
    assert _unregistered_routes(routes, (), written_exemption) == []
