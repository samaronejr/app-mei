"""Which clients a playbook version targets — the `applies_to` half of preview.

Targeting reads the effective-dated registry, never the legacy flag:
`TaxProfile.regime` answers *which rules apply* as of the period being
generated (the bitemporal current row `profile_at` reads), and
`Engagement.service_kind` answers *what the firm sells the client* — the two
axes `applies_to` can name. A client with no profile row covering the period
reads as `unknown`, which is what the S12.1 backfill writes for the clients the
legacy flag could not describe.

Every query runs tenant-scoped — under `tenant_context`, or explicitly keyed by
the caller's tenant — and only ever returns client primary keys: the preview
exists to count, not to browse.
"""

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Final
from uuid import UUID

from django.db.models import Q, QuerySet

from apps.clients.models import (
    ClientCompany,
    ClientStatus,
    Engagement,
    TaxProfile,
    TaxRegime,
)
from apps.cycles.rules import REGIMES, SERVICE_KINDS

if TYPE_CHECKING:
    from apps.tenants.models import Tenant

# A profile gap or an unasserted regime is the UNKNOWN literal, matching the
# backfill's choice: "the system does not know" is a different answer from
# "mei", and must not match the pack's regimes axis by default.
REGIME_UNKNOWN: Final = TaxRegime.UNKNOWN


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


def _regimes_at(
    tenant: "Tenant", clients: list[ClientCompany], on: date
) -> dict[UUID, str]:
    """Map each client to its regime on `on` — `profile_at` read in bulk.

    One query for the whole list rather than N point lookups (the query-budget
    rule): rows arrive latest-first per client and the first one wins, which is
    exactly `profile_at`'s resolution order.
    """
    ids = [client.pk for client in clients]
    rows = (
        # ALL_OBJECTS_OK: the tenant column is in the predicate explicitly —
        # preview runs inside tenant_context in requests, but the same function
        # serves management commands where relying on ambient context would be
        # the silent-empty failure the marker exists to catch.
        TaxProfile.all_objects.filter(
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
    """Map each client to its open engagements' service kinds on `on`.

    "Open" is the engagement's own range — `starts_on <= on`, `ends_on` null or
    after it; the model carries no status column, the date bounds ARE the state.
    """
    ids = [client.pk for client in clients]
    rows = (
        # ALL_OBJECTS_OK: same reason as _regimes_at — tenant_id is in the
        # predicate, so the answer does not depend on ambient context.
        Engagement.all_objects.filter(tenant_id=tenant.pk, client_id__in=ids)
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
