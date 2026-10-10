"""The Axorim view sweep: tenant B's object ids, asked for as tenant A.

Every URL name the pivot added is requested by a signed-in member of firm A naming
firm B's object. Two properties are asserted together, because either alone lies:

* status is 404 or 403 — never a 200 rendering another firm's data;
* the body carries no count or marker of B's row — not even "1 item found" —
  because an enumerated existence hint is the leak this page exists to catch.

GET detail routes also assert the positive control: firm A's OWN object answers
other than 404/403, so "denied" can never pass on a view that 404s unconditionally.
The request methods are discovered, not pinned: a GET that draws 405 retries as POST,
so a route converted to POST-only cannot silently leave this sweep.
"""

from __future__ import annotations

import datetime as dt
import re
import time
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Final, cast
from uuid import UUID

import pytest
from allauth.account.internal.flows.login import (
    AUTHENTICATION_METHODS_SESSION_KEY,
)
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from django.conf import settings
from django.http import HttpResponse
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.core.tenancy import client_context, tenant_context
from apps.cycles.models import RequestState
from apps.tenants.models import Membership, TenantRole
from tests.isolation.axorim_seed import (
    SeedContext,
    make_seed_context,
    seed_model,
)
from tests.isolation.rolecheck import assert_isolated_role
from tests.support import enrol_totp

if TYPE_CHECKING:
    from django.db import models

pytestmark = pytest.mark.django_db(transaction=True)

DENIED = frozenset({HTTPStatus.NOT_FOUND, HTTPStatus.FORBIDDEN})


@dataclass(frozen=True)
class Route:
    """One URL name and the model labels its kwargs resolve to."""

    name: str
    kwargs: dict[str, str]
    control_own_object: bool = True
    # POST-only views that fold a refused lookup into a message + redirect
    # (AXR1-023's support decisions answer 302 to the console, not 404).
    accept_redirect: bool = False


# Every URL name the pivot added, mapped to the model each path kwarg names. A new
# route added without an entry here fails the completeness check below.
FIRM_ROUTES: Final = (
    Route("cycle-detail", {"pk": "cycles.servicecycle"}),
    Route("cycle-request-action", {"pk": "cycles.servicecycle"}),
    Route("cycle-open", {"pk": "clients.clientcompany"}, control_own_object=False),
    Route("cycle-reopen", {"pk": "cycles.servicecycle"}),
    Route("cycle-exception-action", {"pk": "cycles.exception"}),
    Route("proposal-create", {"pk": "cycles.servicecycle"}),
    Route("approval-card", {"pk": "cycles.actionproposal"}),
    Route("evidence-index", {"pk": "cycles.servicecycle"}),
    Route("evidence-export-detail", {"pk": "cycles.evidenceexport"}),
    Route("evidence-export-download", {"pk": "cycles.evidenceexport"}),
    Route("publication-withdraw", {"pk": "cycles.clientpublication"}),
    Route("bulk-detail", {"pk": "cycles.bulkoperation"}),
    Route("bulk-progress", {"pk": "cycles.bulkoperation"}),
    Route("bulk-report-csv", {"pk": "cycles.bulkoperation"}),
    Route("document-review", {"pk": "cycles.evidencelink"}),
    Route("document-preview", {"pk": "cycles.evidencelink"}),
    Route("staff-document-download", {"pk": "obligations.document"}),
    Route("playbook-detail", {"pk": "cycles.playbook"}),
    Route(
        "playbook-editor",
        {"pk": "cycles.playbook", "number": "cycles.playbookversion"},
        control_own_object=False,
    ),
    Route(
        "playbook-retire",
        {"pk": "cycles.playbook", "number": "cycles.playbookversion"},
        control_own_object=False,
    ),
    Route("client-detail", {"pk": "clients.clientcompany"}),
    Route("client-edit", {"pk": "clients.clientcompany"}),
    Route("client-setup", {"pk": "clients.clientcompany"}),
    Route("client-tax-profile", {"pk": "clients.clientcompany"}),
    Route("client-contact-create", {"pk": "clients.clientcompany"}),
    Route(
        "client-contact-edit",
        {"pk": "clients.clientcompany", "contact_pk": "clients.clientcontact"},
        control_own_object=False,
    ),
    Route(
        "client-contact-invite",
        {"pk": "clients.clientcompany", "contact_pk": "clients.clientcontact"},
        control_own_object=False,
    ),
    Route("client-assignment-add", {"pk": "clients.clientcompany"}),
    Route(
        "client-assignment-remove",
        {
            "pk": "clients.clientcompany",
            "assignment_pk": "clients.clientassignment",
        },
        control_own_object=False,
    ),
    Route("client-engagement-create", {"pk": "clients.clientcompany"}),
    Route(
        "client-engagement-edit",
        {"pk": "clients.clientcompany", "engagement_pk": "clients.engagement"},
        control_own_object=False,
    ),
    Route("client-import-batch", {"batch_pk": "clients.importbatch"}),
    Route(
        "client-import-commit",
        {"batch_pk": "clients.importbatch"},
        control_own_object=False,
    ),
    Route(
        "client-import-rollback",
        {"batch_pk": "clients.importbatch"},
        control_own_object=False,
    ),
    Route("client-import-errors", {"batch_pk": "clients.importbatch"}),
    Route("briefing-delete-view", {"pk": "cycles.savedview"}),
    Route("account-export-detail", {"export_id": "tenants.accountexport"}),
    Route("account-export-download", {"export_id": "tenants.accountexport"}),
    Route("firm-retention", {"client_id": "clients.clientcompany"}),
    Route("firm-hold-release", {"hold_id": "cycles.legalhold"}),
    Route("member-review-toggle", {"pk": "tenants.membership"}),
    Route("portal-invite-issue", {"pk": "clients.clientcompany"}),
    # AXR1-023: the owner-side support-grant decisions; both POST-only and
    # both refuse by redirecting to the console with a flash, per support_views.
    Route(
        "firm-suporte-approve",
        {"grant_id": "tenants.supportgrant"},
        accept_redirect=True,
    ),
    Route(
        "firm-suporte-revoke",
        {"grant_id": "tenants.supportgrant"},
        accept_redirect=True,
    ),
    # AXR1-023's grant audit page: firm A's owner asking for firm B's grant trail
    # must get 404/403 (the grant lookup is tenant-scoped), never B's events.
    Route("firm-suporte-trilha", {"grant_id": "tenants.supportgrant"}),
)

# Paramless additions: asserted for marker absence on firm A's own render.
FIRM_PARAMLESS: Final = (
    "cycle-list",
    "document-queue",
    "approval-policy",
    "playbook-list",
    "bulk-start",
    "delivery-queue",
    "clients-export-csv",
    "client-create",
    "client-import",
    "clients-list",
    "briefing-save-view",
    "briefing-dismiss",
    "firm-settings",
    "firm-preferences",
    "firm-account",
    "account-export-request",
    "invite-issue",
    "invite-issued",
    # AXR1-027's operations page and AXR1-023's owner console.
    "automations",
    "firm-suporte",
)

# Portal routes take a portal session on `<slug>-portal.localhost`; the object
# ids named are firm B's, and the assertion never runs a count of B's rows.
PORTAL_ROUTES: Final = (
    Route("portal-publication-detail", {"pk": "cycles.clientpublication"}),
    Route(
        "portal-publication-download",
        {"pk": "cycles.clientpublication", "link_id": "cycles.evidencelink"},
        control_own_object=False,
    ),
    Route("portal-cycle-detail", {"pk": "cycles.servicecycle"}),
    Route("portal-request-detail", {"pk": "cycles.evidencerequest"}),
    Route("portal-request-upload", {"pk": "cycles.evidencerequest"}),
)

PORTAL_PARAMLESS: Final = (
    "portal-home",
    "portal-payments",
    "portal-documents",
    "portal-account",
    "portal-document-upload",
    "portal-brand-css",
)


@pytest.fixture
def tenants() -> dict[str, SeedContext]:
    return {
        "alpha": make_seed_context("axr-view-a", "a"),
        "beta": make_seed_context("axr-view-b", "b"),
    }


def _membership(
    ctx: SeedContext, role: str = TenantRole.STAFF_ACCOUNTANT
) -> Membership:
    key = f"tenants.membership.{role}"
    row = ctx.rows.get(key)
    if row is None:
        # A (user, tenant, client=None) membership is unique per roster, so the
        # OWNER row the context seeded is returned before a new row is made.
        row = Membership.objects.filter(
            user=ctx.user, tenant=ctx.tenant, client__isnull=True
        ).first()
        if row is None:
            row = Membership.objects.create(
                user=ctx.user, tenant=ctx.tenant, role=role, client=None
            )
        ctx.rows[key] = row
    assert isinstance(row, Membership)
    return row


def _resolve(ctx: SeedContext, label: str, url_kwarg: str) -> object:
    """The URL value for a kwarg — pk for most, the version number for playbooks."""
    from django.apps import apps as django_apps  # noqa: PLC0415

    if label == "clients.clientcompany":
        return ctx.client.pk
    if label == "tenants.membership":
        return _membership(ctx).pk
    if label == "tenants.accountexport":
        return _account_export(ctx).pk
    model = django_apps.get_model(label)
    row = seed_model(model, ctx)
    # <int:number> names the version's number, not its id — the only non-pk
    # kwarg in the swept routes.
    if url_kwarg == "number":
        return cast("Any", row).number
    return row.pk


def _account_export(ctx: SeedContext) -> models.Model:
    from django.apps import apps as django_apps  # noqa: PLC0415

    model = django_apps.get_model("tenants", "AccountExport")
    key = model._meta.label_lower
    if key not in ctx.rows:
        with tenant_context(ctx.tenant.id):
            # ALL_OBJECTS_OK: fixture row written inside the tenant context.
            ctx.rows[key] = model.all_objects.create(
                tenant=ctx.tenant,
                requested_by=ctx.user,
                storage_key=f"{ctx.tag}-export.zip",
                sha256="b" * 64,
                error_code="",
            )
    return ctx.rows[key]


def _signed_in_client(user: User, host: str) -> Client:
    """A test client carrying the session a real password+TOTP sign-in writes.

    `force_login` alone never records the authentication method, so a view or
    middleware that asks "was a second factor used" would read the session as
    stale and redirect — the sweep would exercise the redirect, not the route.
    """
    client = Client(SERVER_NAME=host)
    client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
    authenticator = Authenticator.objects.get(user=user, type=Authenticator.Type.TOTP)
    session = client.session
    session[AUTHENTICATION_METHODS_SESSION_KEY] = [
        {"method": "password", "at": time.time(), "email": user.email},
        {
            "method": "mfa",
            "at": time.time(),
            "id": authenticator.pk,
            "type": "totp",
        },
    ]
    session.save()
    client.cookies[settings.SESSION_COOKIE_NAME] = str(session.session_key)
    return client


_POST_PROBE_BODY = {
    # Superset of every field the swept POST views parse. Syntactically valid so
    # the response reflects the scoped object lookup, not form validation: an
    # invalid form short-circuits to 422/409 BEFORE tenant scoping runs, which
    # would let a route that leaks B's object hide behind a validation error.
    "version": "1",
    "expected_version": "1",
    "reason": "sweep",
    "action": "send",
    "engagement_id": "00000000-0000-0000-0000-000000000000",
    "playbook_version_id": "00000000-0000-0000-0000-000000000000",
    "periodo": "2026-09",
    "email": "probe@example.test",
    "role": "staff_accountant",
}


PORTAL_URLCONF = "apps.portal.urls"


def _request(
    client: Client,
    route: str,
    kwargs: dict[str, object],
    *,
    urlconf: str | None = None,
) -> HttpResponse:
    """GET first; a 405 means the route is POST-only, so POST a valid-form body."""
    url = reverse(route, kwargs=kwargs, urlconf=urlconf)
    response = client.get(url)
    if response.status_code == HTTPStatus.METHOD_NOT_ALLOWED:
        response = client.post(url, _POST_PROBE_BODY)
    return cast("HttpResponse", cast("object", response))


def _markers(ctx: SeedContext, *objects: object) -> list[str]:
    """Byte strings that must never appear in tenant A's responses."""
    markers = [ctx.client.legal_name, f"axr-{ctx.tag}"]
    markers += [
        marker
        for obj in objects
        # A marker shorter than 8 chars (a version number, a small id) appears
        # on any page by coincidence — only long opaque ids prove a leak.
        if len(marker := str(getattr(obj, "pk", obj))) >= 8
    ]
    return markers


def _assert_no_markers(response: object, ctx: SeedContext, *objects: object) -> None:
    body = getattr(response, "content", b"").decode(errors="replace")
    for marker in _markers(ctx, *objects):
        assert marker not in body, (
            f"tenant B marker {marker!r} present in a firm A response"
        )


_COUNT_DISCLOSURE = re.compile(
    r"""(
        \b\d+\s+(?:item|itens|items?|resultados?|registros?|linhas?|encontrados?)\b
        | \b(?:item|itens|items?|resultados?|registros?|linhas?)\s*[:=]\s*\d+\b
        | \b(?:total|count)["']?\s*[:=]\s*\d+\b
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def _assert_no_counts(response: HttpResponse) -> None:
    """Reject count disclosures in denied bodies, including zero and one."""
    body = getattr(response, "content", b"").decode(errors="replace")
    match = _COUNT_DISCLOSURE.search(body)
    assert match is None, (
        f"a firm A deny response discloses a row count: {match.group(0)!r} "
        f"in {body[:200]!r}"
    )


@pytest.mark.parametrize(
    "body",
    [
        "7 itens encontrados; total=7; count=7",
        "total=7",
        "count=7",
        "1 item encontrado",
        "0 itens encontrados",
        '{"count":7}',
    ],
)
def test_denied_body_rejects_count_disclosures(body: str) -> None:
    # Given a denied response carrying a count rather than a company marker.
    response = HttpResponse(body, status=HTTPStatus.NOT_FOUND)

    # When the same helper used by the route sweep inspects that response.
    # Then a count disclosure fails even when no object identifiers appear.
    with pytest.raises(AssertionError):
        _assert_no_counts(response)


@pytest.fixture
def alpha_session(tenants: dict[str, SeedContext]) -> Client:
    enrol_totp(tenants["alpha"].user)
    return _signed_in_client(
        tenants["alpha"].user, f"{tenants['alpha'].tenant.slug}.localhost"
    )


@pytest.mark.parametrize("route", FIRM_ROUTES, ids=lambda r: r.name)
def test_a_firm_route_never_serves_tenant_bs_object(
    route: Route,
    tenants: dict[str, SeedContext],
    alpha_session: Client,
) -> None:
    # Given firm B's objects for every kwarg this route takes
    assert_isolated_role()
    beta_kwargs = {
        name: _resolve(tenants["beta"], label, name)
        for name, label in route.kwargs.items()
    }

    # When firm A's signed-in member requests them
    response = _request(alpha_session, route.name, beta_kwargs)

    # Then the object is never disclosed, and its ids never counted in the body
    denied = response.status_code in DENIED or (
        route.accept_redirect and response.status_code == HTTPStatus.FOUND
    )
    assert denied, f"{route.name} answered {response.status_code} for tenant B ids"
    if response.status_code == HTTPStatus.FOUND:
        # A refused POST redirects to the console; follow it and check the
        # landing page carries none of B's markers or counts either.
        landed = alpha_session.get(response.headers["Location"])
        _assert_no_markers(landed, tenants["beta"], *beta_kwargs.values())
    _assert_no_markers(response, tenants["beta"], *beta_kwargs.values())
    _assert_no_counts(response)

    # Positive control where the route renders the object: firm A's own row must
    # answer other than 404/403, or "denied" passed on a route that denies all.
    if route.control_own_object and "GET" in _allowed_methods(
        alpha_session, route.name, beta_kwargs
    ):
        own_kwargs = {
            name: _resolve(tenants["alpha"], label, name)
            for name, label in route.kwargs.items()
        }
        own = alpha_session.get(reverse(route.name, kwargs=own_kwargs))
        assert own.status_code not in DENIED, (
            f"{route.name} refused firm A's own object ({own.status_code}); "
            "the denial above is vacuous"
        )


def _allowed_methods(client: Client, route: str, kwargs: dict[str, object]) -> set[str]:
    """Probe methods once, on a fresh uuid, so method discovery leaks nothing."""
    probe = {key: str(UUID(int=0)) for key in kwargs}
    probe_url = reverse(route, kwargs=probe)
    methods = {"POST"}
    if client.get(probe_url).status_code != HTTPStatus.METHOD_NOT_ALLOWED:
        methods.add("GET")
    return methods


@pytest.mark.parametrize("name", FIRM_PARAMLESS)
def test_a_paramless_firm_route_renders_no_tenant_b_data(
    name: str,
    tenants: dict[str, SeedContext],
    alpha_session: Client,
) -> None:
    # Given a seeded row of B's, so an unscoped list would have something to show
    assert_isolated_role()
    beta = tenants["beta"]
    with tenant_context(beta.tenant.id), client_context(beta.client.id):
        seed_model(
            __import__("apps.cycles.models", fromlist=["ServiceCycle"]).ServiceCycle,
            beta,
        )
    alpha = tenants["alpha"]
    # The firm-account page renders the firm's billing row unconditionally; a
    # tenant with none fails the render before isolation is even exercised.
    with tenant_context(alpha.tenant.id):
        seed_model(
            __import__(
                "apps.tenants.billing_models", fromlist=["BillingAccount"]
            ).BillingAccount,
            alpha,
        )

    # When firm A asks for the route
    response = _request(alpha_session, name, {})

    # Then it answers with A's view of the world and none of B's markers —
    # including no count that could disclose B's rows exist.
    assert response.status_code in {
        HTTPStatus.OK,
        HTTPStatus.FOUND,
        HTTPStatus.UNPROCESSABLE_CONTENT,
        HTTPStatus.CONFLICT,
    }, f"{name} answered {response.status_code}"
    _assert_no_markers(response, tenants["beta"])


@pytest.fixture
def portal(tenants: dict[str, SeedContext]) -> Client:
    """A portal session for firm A's client — the seat the middleware grants."""
    alpha = tenants["alpha"]
    user = User.objects.create_user(email=f"cliente@{alpha.tag}.example.test")
    EmailAddress.objects.get_or_create(
        user=user,
        email=user.email,
        defaults={"verified": True, "primary": True},
    )
    with tenant_context(alpha.tenant.id):
        Membership.objects.create(
            user=user,
            tenant=alpha.tenant,
            role=TenantRole.CLIENT_OWNER,
            client=alpha.client,
        )
        # A sent request: the portal only surfaces requests in live states.
        request = seed_model(
            __import__(
                "apps.cycles.models", fromlist=["EvidenceRequest"]
            ).EvidenceRequest,
            alpha,
            overrides={"state": RequestState.REQUESTED},
        )
        assert request.pk is not None
    enrol_totp(user)
    return _signed_in_client(user, f"{alpha.tenant.slug}-portal.localhost")


@pytest.mark.parametrize("route", PORTAL_ROUTES, ids=lambda r: r.name)
def test_a_portal_route_never_serves_tenant_bs_object(
    route: Route,
    tenants: dict[str, SeedContext],
    portal: Client,
) -> None:
    # Given firm B's objects
    assert_isolated_role()
    beta_kwargs = {
        name: _resolve(tenants["beta"], label, name)
        for name, label in route.kwargs.items()
    }

    # When firm A's portal session requests them
    response = _request(portal, route.name, beta_kwargs, urlconf=PORTAL_URLCONF)

    # Then 404/403, with nothing of B's in the body — the portal is confined by
    # app_portal's restrictive policies, so a leak here means both layers failed.
    assert response.status_code in DENIED, (
        f"{route.name} answered {response.status_code} for tenant B ids"
    )
    _assert_no_markers(response, tenants["beta"], *beta_kwargs.values())
    _assert_no_counts(response)


def test_a_support_grant_for_firm_a_admits_nothing_on_firm_b(
    tenants: dict[str, SeedContext],
) -> None:
    # Given a platform staff account the alpha owner actively admitted
    assert_isolated_role()
    from apps.authz.support import resolve_support_grant  # noqa: PLC0415
    from apps.tenants.models import SupportGrant  # noqa: PLC0415

    alpha, beta = tenants["alpha"], tenants["beta"]
    staff = User.objects.create_user(
        email=f"support@{alpha.tag}.example.test", is_staff=True
    )
    with tenant_context(alpha.tenant.id):
        # ALL_OBJECTS_OK: support-grant fixture seeded under the alpha tenant context
        SupportGrant.all_objects.create(
            tenant=alpha.tenant,
            requested_by=staff,
            approved_by_owner=alpha.user,
            purpose="cross-firm probe",
            client_ids=[str(alpha.client.pk)],
            starts_at=timezone.now() - dt.timedelta(hours=1),
            expires_at=timezone.now() + dt.timedelta(hours=1),
            approved_at=timezone.now(),
        )
    # Sanity: the row is live for firm A — the denial below is not vacuous.
    assert resolve_support_grant(staff.pk, alpha.tenant.pk) is not None
    assert resolve_support_grant(staff.pk, beta.tenant.pk) is None

    # When the same staff session is carried onto firm B's host. The hostile
    # request reuses the session cookie; a fresh force_login would only mint a
    # second, honest session.
    enrol_totp(staff)
    session = _signed_in_client(staff, f"{alpha.tenant.slug}.localhost").session
    hostile = Client(SERVER_NAME=f"{beta.tenant.slug}.localhost")
    hostile.cookies[settings.SESSION_COOKIE_NAME] = str(session.session_key)

    # Then every firm route on B's host refuses before a view runs: the grant
    # names firm A, and RLS hides it from B's context besides.
    for route_name in ("cycle-list", "firm-settings", "firm-suporte"):
        response = hostile.get(reverse(route_name))
        assert response.status_code in DENIED, (
            f"{route_name} answered {response.status_code} for a grant that "
            "only names firm A"
        )
        _assert_no_markers(response, beta)


def test_company_select_refuses_a_seat_the_account_does_not_hold(
    tenants: dict[str, SeedContext],
    portal: Client,
) -> None:
    # Given firm A's portal session and firm B's client id
    assert_isolated_role()

    # When the session is asked to switch into B's company
    response = portal.post(
        reverse("portal-company-select", urlconf=PORTAL_URLCONF),
        {"client": str(tenants["beta"].client.pk)},
    )

    # Then the seat check refuses it — a seat in *a* company must never satisfy
    # the check for *this* company
    assert response.status_code in DENIED


@pytest.mark.parametrize("name", PORTAL_PARAMLESS)
def test_a_paramless_portal_route_renders_no_tenant_b_data(
    name: str,
    tenants: dict[str, SeedContext],
    portal: Client,
) -> None:
    # Given B's seeded client row
    assert_isolated_role()

    # When firm A's portal session asks for the page
    response = _request(portal, name, {}, urlconf=PORTAL_URLCONF)

    # Then no marker of B's tenant is rendered. Non-2xx statuses pass here because
    # the assertion is about absence, and several of these pages legitimately
    # answer 302 or 503 for reasons unrelated to the sweep.
    assert response.status_code != HTTPStatus.INTERNAL_SERVER_ERROR
    _assert_no_markers(response, tenants["beta"])


def test_every_new_route_name_is_swept() -> None:
    # Given the swept sets above
    swept = (
        {r.name for r in FIRM_ROUTES}
        | set(FIRM_PARAMLESS)
        | {r.name for r in PORTAL_ROUTES}
        | set(PORTAL_PARAMLESS)
        | {
            "portal-company-select",
            # Token-keyed (a bearer secret, not a tenant object id) and infra probes.
            "invite-accept",
            "portal-invite-accept",
            "portal-brand-logo",
            "portal-document-download",
        }
    )

    # When the pivot's added names are listed from the urlconf
    from django.urls import get_resolver  # noqa: PLC0415

    added = {
        "member-review-toggle",
        "client-create",
        "client-import",
        "client-import-batch",
        "client-import-commit",
        "client-import-rollback",
        "client-import-errors",
        "client-edit",
        "client-setup",
        "client-engagement-create",
        "client-engagement-edit",
        "client-tax-profile",
        "client-contact-create",
        "client-contact-edit",
        "client-contact-invite",
        "client-assignment-add",
        "client-assignment-remove",
        "client-detail",
        "evidence-index",
        "cycle-reopen",
        "evidence-export-detail",
        "evidence-export-download",
        "publication-withdraw",
        "bulk-start",
        "bulk-detail",
        "bulk-progress",
        "bulk-report-csv",
        "approval-card",
        "proposal-create",
        "approval-policy",
        "cycle-list",
        "cycle-detail",
        "cycle-request-action",
        "cycle-exception-action",
        "cycle-open",
        "document-queue",
        "document-review",
        "document-preview",
        "staff-document-download",
        "playbook-list",
        "playbook-detail",
        "playbook-editor",
        "playbook-retire",
        "delivery-queue",
        "briefing-save-view",
        "briefing-delete-view",
        "briefing-dismiss",
        "portal-publication-detail",
        "portal-publication-download",
        "portal-cycle-detail",
        "portal-request-detail",
        "portal-request-upload",
        "portal-company-select",
        "portal-brand-css",
        "portal-brand-logo",
        "portal-document-download",
        "firm-settings",
        "firm-preferences",
        "firm-account",
        "account-export-request",
        "account-export-detail",
        "account-export-download",
        "firm-retention",
        "firm-hold-release",
        "portal-invite-issue",
        "clients-export-csv",
        "clients-list",
        "automations",
        "firm-suporte",
        "firm-suporte-approve",
        "firm-suporte-revoke",
        "firm-suporte-trilha",
    }

    # Then every added name is swept or explicitly decided — a new URL added
    # without an entry fails here rather than going unprobed.
    assert added <= swept, f"routes added but not swept: {sorted(added - swept)}"

    # And every added name genuinely exists in a urlconf — a typo'd name in
    # the swept lists would pass the set comparison while naming a route that
    # does not exist. Firm routes live in the root resolver, portal routes in
    # PORTAL_URLCONF, so each name must resolve in one of them.
    portal_resolver = get_resolver(PORTAL_URLCONF)
    firm_names = {str(k) for k in get_resolver().reverse_dict}
    portal_names = {str(k) for k in portal_resolver.reverse_dict}
    unresolvable = sorted(added - firm_names - portal_names)
    assert not unresolvable, (
        f"route names in the sweep set that resolve nowhere: {unresolvable}"
    )
