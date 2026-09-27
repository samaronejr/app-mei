"""Which clients a playbook version targets — the `applies_to` half of preview.

The registry columns this reads (`TaxProfile.regime`, `Engagement.service_kind`)
land with T5, which is a sibling of this change, not part of its base. The
readers therefore degrade along exactly the same mapping T5's own backfill
applies (S12.1): `is_mei` reads as regime `mei` and service kind `mei_mensal`,
anything else as `unknown`/`documentos_mensal`. When the models are installed
they are used and the mapping never runs; the two paths cannot disagree on the
same database because a client either has backfilled rows or falls back to the
flag the backfill read.

Every query runs tenant-scoped — under `tenant_context`, or explicitly keyed by
the caller's tenant — and only ever returns client primary keys: the preview
exists to count, not to browse.
"""

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, Final, cast
from uuid import UUID

from django.apps import apps as django_apps
from django.db.models import Model, Q, QuerySet

from apps.clients.models import ClientCompany, ClientStatus
from apps.cycles.rules import REGIMES, SERVICE_KINDS

if TYPE_CHECKING:
    from apps.tenants.models import Tenant

# Same literals as rules.py's tuples, named here for readability at the use site.
REGIME_MEI: Final = "mei"
REGIME_UNKNOWN: Final = "unknown"
KIND_MEI_MENSAL: Final = "mei_mensal"
KIND_DOCUMENTOS_MENSAL: Final = "documentos_mensal"


@dataclass(frozen=True, slots=True)
class AppliesTo:
    """A parsed `applies_to` scope: empty sets match everything on that axis."""

    regimes: frozenset[str]
    service_kinds: frozenset[str]

    @classmethod
    def parse(cls, raw: object) -> "AppliesTo":
        """Build the scope from stored JSON; callers validate before storing.

        Stored rows passed `validate_applies_to` at write, so a value read back
        here is already one of the known literals — a raw call with untrusted
        input goes through `rules.validate_applies_to`, not this.
        """
        if not isinstance(raw, dict):
            return cls(regimes=frozenset(), service_kinds=frozenset())

        def axis(key: str, permitted: tuple[str, ...]) -> frozenset[str]:
            """Read one axis; a stored malformed value fails closed, never wider.

            A non-list here means a write path bypassed validation, and treating
            it as the empty set would read as *unrestricted* — the permissive
            direction — so the scope narrows to nothing instead.
            """
            values = raw.get(key)
            if values is None:
                return frozenset()
            if not isinstance(values, list):
                return frozenset({"\x00invalid"})
            parsed = {str(value) for value in values}
            if parsed - set(permitted):
                # Dropping invalid entries could empty the set back to
                # unrestricted; unknown literals narrow to a never-match.
                return frozenset({"\x00invalid"})
            return frozenset(parsed)

        return cls(
            regimes=axis("regimes", REGIMES),
            service_kinds=axis("service_kinds", SERVICE_KINDS),
        )


def _all_objects(model: type[Model]) -> Any:  # noqa: ANN401 — see docstring
    """Return the unscoped manager of an optionally-installed sibling model.

    Every tenant table carries `all_objects`, but `Model` itself does not —
    the attribute exists by codebase convention (TenantScopedModel), so mypy
    needs the explicit escape for a model type it has never seen. `Any` is the
    honest return: the concrete class is a sibling todo's private surface.
    """
    # ALL_OBJECTS_OK: every caller filters on the explicit tenant — see _regimes_at.
    return cast("Any", model).all_objects


def _optional_model(app_label: str, name: str) -> type[Model] | None:
    """Return the model, or None when the sibling todo that ships it is absent.

    `apps.get_model` raises LookupError for a model a registered app does not
    have — which is exactly the state of this branch's base: `clients` is
    installed, `TaxProfile` and `Engagement` are T5's, not yet here.
    """
    try:
        return django_apps.get_model(app_label, name)
    except LookupError:
        return None


def _regimes_at(
    tenant: "Tenant", clients: list[ClientCompany], on: date
) -> dict[UUID, str]:
    """Map each client to its regime on `on`, reading TaxProfile when present.

    Without the model — this change's own base — the answer is the one T5's
    backfill derives from the same flag, so the preview a firm sees today is the
    count the backfilled registry will reproduce.
    """
    model = _optional_model("clients", "TaxProfile")
    if model is None:
        return {
            client.pk: REGIME_MEI if client.is_mei else REGIME_UNKNOWN
            for client in clients
        }
    ids = [client.pk for client in clients]
    rows = (
        # ALL_OBJECTS_OK: the tenant column is in the predicate explicitly —
        # preview runs inside tenant_context in requests, but the same function
        # serves management commands where relying on ambient context would be
        # the silent-empty failure the marker exists to catch.
        _all_objects(model)
        .filter(
            tenant_id=tenant.pk,
            client_id__in=ids,
            superseded_at__isnull=True,
            valid_from__lte=on,
        )
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gt=on))
        .order_by("client_id", "-valid_from")
        .values_list("client_id", "regime")
    )
    regimes: dict[UUID, str] = {}
    for client_id, regime in rows:
        # Rows arrive latest-first per client; the first one seen wins, exactly
        # as `profile_at` resolves it.
        regimes.setdefault(client_id, str(regime))
    return {client.pk: regimes.get(client.pk, REGIME_UNKNOWN) for client in clients}


def _service_kinds_at(
    tenant: "Tenant", clients: list[ClientCompany], on: date
) -> dict[UUID, frozenset[str]]:
    """Map each client to its open engagements' service kinds on `on`."""
    model = _optional_model("clients", "Engagement")
    if model is None:
        return {
            client.pk: frozenset(
                {KIND_MEI_MENSAL} if client.is_mei else {KIND_DOCUMENTOS_MENSAL}
            )
            for client in clients
        }
    ids = [client.pk for client in clients]
    rows = (
        # ALL_OBJECTS_OK: same reason as _regimes_at — tenant_id is in the
        # predicate, so the answer does not depend on ambient context.
        _all_objects(model)
        .filter(tenant_id=tenant.pk, client_id__in=ids)
        .filter(Q(starts_on__lte=on))
        .filter(Q(ends_on__isnull=True) | Q(ends_on__gt=on))
        .values_list("client_id", "service_kind")
    )
    kinds: dict[UUID, set[str]] = {}
    for client_id, service_kind in rows:
        kinds.setdefault(client_id, set()).add(str(service_kind))
    return {client.pk: frozenset(kinds.get(client.pk, ())) for client in clients}


def matching_clients(
    tenant: "Tenant",
    applies_to: object,
    on: date,
) -> QuerySet[ClientCompany]:
    """Return the active clients of `tenant` the scope covers at `on`.

    A cycle is generated per matching engagement (T9), so a client counts once
    even when several engagements qualify — the preview answers "for how many
    clients does this fire", which is the number a publisher can sanity-check.
    """
    scope = AppliesTo.parse(applies_to)
    clients = list(
        # ALL_OBJECTS_OK: keyed on the explicit tenant — see _regimes_at.
        ClientCompany.all_objects.filter(
            tenant_id=tenant.pk,
            status=ClientStatus.ACTIVE,
        ).order_by("pk"),
    )
    if not clients:
        # ALL_OBJECTS_OK: none() reads nothing; the hatch is cosmetic here.
        return ClientCompany.all_objects.none()
    regimes = _regimes_at(tenant, clients, on)
    service_kinds = _service_kinds_at(tenant, clients, on)
    matched = [
        client.pk
        for client in clients
        if (not scope.regimes or regimes[client.pk] in scope.regimes)
        and (not scope.service_kinds or service_kinds[client.pk] & scope.service_kinds)
    ]
    # ALL_OBJECTS_OK: pks were computed tenant-keyed above; this narrows,
    # never widens.
    return ClientCompany.all_objects.filter(pk__in=matched).order_by("pk")
