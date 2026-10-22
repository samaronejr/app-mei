"""A populated client-period for the regional read budgets (AXRM-004).

This todo builds it from R1 synthetic data only: `seed_axorim_demo` (the data
behind the S11.2 baseline) for its portal client's latest competence, plus
enough requests sent through the real services that any per-row lookup on
either read breaches the 25-query budget. Later regional todos add their
section's rows in `regional_world` and their rendered-row witness in
`RegionalWorld.assert_populated`, so a budget can never pass on an empty section.
"""

from dataclasses import dataclass
from io import StringIO
from typing import TYPE_CHECKING, Final
from uuid import UUID

from django.core.management import call_command
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.cycles.models import EvidenceRequest, ServiceCycle
from apps.cycles.services.requests import RequestFields, create_request
from apps.tenants.models import Membership, Tenant
from ops.perf.results import QUERIES
from tests.browser.harness import mfa_session_cookie

if TYPE_CHECKING:
    from django.test.client import _MonkeyPatchedWSGIResponse

REGIONAL_READS: Final = ("workspace", "cycle_detail")
SEED_FIRM: Final = "small"
SEED: Final = 42
# More rows than the budget allows queries, so one lookup per row cannot hide.
POPULATED_REQUESTS: Final = QUERIES + 5


@dataclass(frozen=True, slots=True)
class RegionalWorld:
    """One seeded firm's populated client-period and the rows each read renders."""

    tenant: Tenant
    owner: User
    client: ClientCompany
    cycle: ServiceCycle
    requests: frozenset[UUID]

    def firm(self) -> Client:
        """An MFA firm-host session for the seeded owner."""
        host = f"{self.tenant.slug}.localhost"
        browser = Client(SERVER_NAME=host)
        cookie = mfa_session_cookie(self.owner, f"http://{host}/")
        browser.cookies[cookie["name"]] = cookie["value"]
        return browser

    def path(self, read: str) -> str:
        """The named read's URL for this client-period."""
        if read == "workspace":
            return (
                reverse("client-detail", args=[self.client.pk])
                + f"?periodo={self.cycle.period_start:%Y-%m}"
            )
        if read == "cycle_detail":
            return reverse("cycle-detail", args=[self.cycle.pk])
        message = f"Unknown regional read: {read}"
        raise ValueError(message)

    def assert_populated(
        self, read: str, response: "_MonkeyPatchedWSGIResponse"
    ) -> None:
        """Require every populated row in context and in the rendered HTML."""
        context = response.context
        assert context is not None
        if read == "workspace":
            listed = {row.request_id for row in context["period_requests"]}
        else:
            listed = {row["request"].pk for row in context["evidence_requests"]}
        assert listed == self.requests, (read, len(listed), len(self.requests))
        missing = [
            pk
            for pk in self.requests
            if f'data-request-id="{pk}"'.encode() not in response.content
        ]
        assert missing == [], (read, missing)


def regional_world() -> RegionalWorld:
    """Seed the R1 synthetic firm, then populate its portal client's competence.

    The caller points MEDIA_ROOT at a temporary directory: the seed writes its
    synthetic document files to local storage.
    """
    call_command(
        "seed_axorim_demo", f"--firms={SEED_FIRM}", f"--seed={SEED}", stdout=StringIO()
    )
    # PLATFORM_QUERY_OK: the test resolves its own seeded synthetic firm.
    tenant = Tenant.objects.get(slug=f"axorim-{SEED_FIRM}-{SEED}")
    owner = User.objects.get(email=f"owner@{tenant.slug}.example.com")
    portal = User.objects.get(email=f"portal@{tenant.slug}.example.com")
    client_id = Membership.objects.get(user=portal, tenant=tenant).client_id
    assert client_id is not None
    with tenant_context(tenant.pk):
        client = ClientCompany.objects.get(pk=client_id)
        cycle = ServiceCycle.objects.filter(client=client).latest("period_start")
        for index in range(POPULATED_REQUESTS):
            cycle.refresh_from_db()
            create_request(
                owner,
                cycle.pk,
                RequestFields(f"Regional budget request {index}"),
                expected_version=cycle.version,
            )
        cycle.refresh_from_db()
        requests = frozenset(
            EvidenceRequest.objects.filter(cycle=cycle).values_list("pk", flat=True)
        )
        assert not EvidenceRequest.objects.filter(
            client=client, cycle__period_start=cycle.period_start
        ).exclude(cycle=cycle)
    assert len(requests) > QUERIES
    return RegionalWorld(tenant, owner, client, cycle, requests)
