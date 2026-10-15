"""Per-tenant feature flags: every R1 surface ships dark and turns on per cohort.

Two independent answers compose into one yes/no:

* **`Tenant.features`** is the per-tenant entitlement: a list of slugs the firm's
  cohort has earned. It is data, not code — a cohort move is an UPDATE, never a
  deploy, and `Tenant.plan` is never branched on.
* **`settings.AXORIM_DISABLED_FEATURES`** is the global kill switch. It overrides
  the row: a flag named there is off for every tenant whatever they carry, so a
  misbehaving surface can be stood down platform-wide without a migration.

Where the answer comes from depends on who is asking, and each hop is deliberate:

* **Inside a portal request**, after `SET LOCAL ROLE app_portal`, `tenants_tenant`
  is denied to the effective role. `PortalMiddleware` resolves the flags into
  `PortalStash.features` before the switch, so `tenant_has` answers from the stash
  and performs no query at all.
* **Inside a firm request**, `TenantMiddleware` opens `_flag_cache` and primes it
  from the row it already resolved, so the request's own tenant answers for free
  and every other tenant is read once per request, never once per question.
* **Outside a request** — a beat task iterating tenants, a management command —
  there is no scope to cache in, so `tenant_has` reads the row directly. That is
  the honest answer, not a missing optimization.

An unknown slug raises `UnknownFeature` rather than answering `False`, for the same
reason `UnknownCapability` exists in the permission matrix: "flag off" and
"flag misspelled" must never be the same answer, or a typo'd gate reads as a
working refusal while letting nothing through.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Concatenate, Final, ParamSpec, TypeVar
from uuid import UUID

from django.conf import settings
from django.http import Http404, HttpRequest
from django.http.response import HttpResponseBase

from apps.authz.stash import stashed_features
from apps.tenants.models import Tenant

# The R1 rollout registry. A flag not in this set cannot be granted to a tenant,
# cannot gate a view, and cannot be answered about: every layer refuses it loudly
# so a rename never strands a cohort on a dead slug.
FEATURES: Final[frozenset[str]] = frozenset(
    {
        # T9+: the client-period cycle itself — requests, uploads, outcomes.
        "cycles_r1",
        # T19: the daily "Hoje" exception briefing.
        "briefing_r1",
        # T24: shadow AI evaluation; produces proposals nothing acts on.
        "ai_shadow",
        # T20: bulk operations across clients.
        "bulk_r1",
        # T7: import of an existing client base into the product.
        "import_r1",
        # T22: firm settings, account lifecycle and private usage reporting.
        "firm_settings_r1",
    }
)


class UnknownFeature(LookupError):  # noqa: N818
    """A call site named a flag the registry does not define.

    An exception rather than a `False`: a typo'd slug would otherwise read as
    "this tenant may not", which is indistinguishable from a working denial and
    would hide a permanently broken gate behind a plausible 404.
    """


# None means "no scope is open" — a background caller — and falls through to a
# direct read. A dict means a request is in flight and owns this lifetime:
# TenantMiddleware opens it, primes it with the resolved tenant, and resets it on
# the way out, so the answers it holds can never outlive the request.
_flag_cache: ContextVar[dict[UUID, frozenset[str]] | None] = ContextVar(
    "tenant_flag_cache",
    default=None,
)


@contextmanager
def open_flag_cache() -> Iterator[None]:
    """Open the per-request flag cache for the caller's scope, then close it.

    `TenantMiddleware` opens one around every request that resolves a tenant; a
    beat task looping tenants may open one per run. token/reset, never a bare
    set(): one thread serves many requests and a leaked cache would answer the
    next request with this one's flags.
    """
    token = _flag_cache.set({})
    try:
        yield
    finally:
        _flag_cache.reset(token)


def prime_flag_cache(tenant: Tenant) -> None:
    """Seed the open cache from a tenant row the caller already holds.

    `TenantMiddleware` resolves the row for routing before the view runs; priming
    from it means the request's own tenant answers every flag check without a
    query of its own. A no-op when no cache is open.
    """
    cache = _flag_cache.get()
    if cache is not None:
        cache[tenant.pk] = frozenset(tenant.features)


def _disabled_features() -> frozenset[str]:
    """Read the kill switch per call so `override_settings` and deploys both bind."""
    return frozenset(getattr(settings, "AXORIM_DISABLED_FEATURES", ()))


def _read_features(tenant_id: UUID) -> frozenset[str]:
    # PLATFORM_QUERY_OK: the entitlement read itself. tenants_tenant carries no
    # policy by design — the middleware resolves it before any context exists — and
    # this query is keyed on the tenant it is answering about, never on a caller's
    # input. A missing row resolves to no flags: an id that names nothing grants
    # nothing.
    row = Tenant.objects.filter(pk=tenant_id).values_list("features", flat=True).first()
    return frozenset(row or ())


def _features_for(tenant_id: UUID) -> frozenset[str]:
    """Resolve one tenant's raw flag set: stash, then request cache, then the row."""
    stashed = stashed_features(tenant_id)
    if stashed is not None:
        return stashed
    cache = _flag_cache.get()
    if cache is None:
        return _read_features(tenant_id)
    if tenant_id not in cache:
        cache[tenant_id] = _read_features(tenant_id)
    return cache[tenant_id]


def tenant_has(tenant_id: UUID, flag: str) -> bool:
    """Report whether `flag` is on for the tenant, with the kill switch applied.

    `flag` must name a member of `FEATURES`: a slug outside the registry raises
    `UnknownFeature` rather than answering False, so a typo'd gate cannot pass as
    a refusal.
    """
    if flag not in FEATURES:
        msg = (
            f"{flag!r} is not a feature flag in FEATURES. Answering False here "
            f"would hide a permanently broken gate behind a plausible 404."
        )
        raise UnknownFeature(msg)
    if flag in _disabled_features():
        return False
    return flag in _features_for(tenant_id)


def tenant_row_has(tenant: Tenant, flag: str) -> bool:
    """Answer `tenant_has` from a row already in memory, issuing no query.

    For `PortalMiddleware`, which holds the resolved tenant row before any stash or
    flag cache exists and must not spend a query per request on the question.
    """
    if flag not in FEATURES:
        msg = f"{flag!r} is not a feature flag in FEATURES."
        raise UnknownFeature(msg)
    return flag not in _disabled_features() and flag in (tenant.features or ())


_P = ParamSpec("_P")
_ResponseT = TypeVar("_ResponseT", bound=HttpResponseBase)
_RequestT = TypeVar("_RequestT", bound=HttpRequest)


def _check_flag(flag: str) -> None:
    """Fail at decoration time, not request time, when the slug is unknown.

    Evaluated when the module defining the view is imported: a typo'd flag is a
    startup crash, not a latent 404.
    """
    if flag not in FEATURES:
        msg = (
            f"require_feature({flag!r}) names no flag in FEATURES. The gate would "
            f"404 forever and read as a working refusal."
        )
        raise UnknownFeature(msg)


def require_feature(
    flag: str,
) -> Callable[
    [Callable[Concatenate[_RequestT, _P], _ResponseT]],
    Callable[Concatenate[_RequestT, _P], _ResponseT],
]:
    """Gate a view on a tenant flag: the surface is absent while the flag is off.

    404 rather than 403 on purpose: a flag-off tenant must not learn that the
    surface exists. `request.tenant` is the GRANTED tenant — middleware attaches it
    only after authorization — so anonymous requests and platform hosts, which
    carry no tenant, are refused rather than resolved into one.
    """
    _check_flag(flag)

    def decorate(
        view: Callable[Concatenate[_RequestT, _P], _ResponseT],
    ) -> Callable[Concatenate[_RequestT, _P], _ResponseT]:
        @wraps(view)
        def guarded(
            request: _RequestT,
            /,
            *args: _P.args,
            **kwargs: _P.kwargs,
        ) -> _ResponseT:
            tenant = getattr(request, "tenant", None)
            if tenant is None or not tenant_has(tenant.pk, flag):
                raise Http404
            return view(request, *args, **kwargs)

        return guarded

    return decorate


def require_portal_feature(
    flag: str,
) -> Callable[
    [Callable[Concatenate[_RequestT, _P], _ResponseT]],
    Callable[Concatenate[_RequestT, _P], _ResponseT],
]:
    """Gate a PORTAL view on a tenant flag, answered entirely from the stash.

    After `SET LOCAL ROLE app_portal`, `tenants_tenant` is denied to the effective
    role — a lookup would abort the transaction rather than refuse — so
    `PortalMiddleware` resolves the flags into `PortalStash.features` before the
    switch and this decorator asks the stash alone, performing no query. No stash
    means no portal context was granted, which is a refusal, never a fallback read.
    """
    _check_flag(flag)

    def decorate(
        view: Callable[Concatenate[_RequestT, _P], _ResponseT],
    ) -> Callable[Concatenate[_RequestT, _P], _ResponseT]:
        @wraps(view)
        def guarded(
            request: _RequestT,
            /,
            *args: _P.args,
            **kwargs: _P.kwargs,
        ) -> _ResponseT:
            stash_features = stashed_features(_portal_tenant_id(request))
            if (
                stash_features is None
                or flag in _disabled_features()
                or flag not in stash_features
            ):
                raise Http404
            return view(request, *args, **kwargs)

        return guarded

    return decorate


def _portal_tenant_id(request: HttpRequest) -> UUID | None:
    """Return the tenant the portal middleware granted this request, if any.

    `stashed_features` needs the tenant to bind the answer to the firm the stash
    was built for; a request that never received one — anonymous, unconfined —
    cannot be answered about and resolves to None, which the caller refuses.
    """
    tenant = getattr(request, "tenant", None)
    return tenant.pk if tenant is not None else None
