"""Small real tenant graph for cycle contracts; no mocked database boundaries."""

from dataclasses import dataclass
from datetime import date

from apps.accounts.models import User
from apps.clients.models import ClientCompany, Engagement, TaxProfile
from apps.core.tenancy import tenant_context
from apps.cycles.models import PlaybookVersion
from apps.cycles.packs import install_system_packs
from apps.tenants.models import Membership, Tenant, TenantRole

PERIOD = date(2026, 9, 1)


@dataclass(frozen=True)
class CycleSetup:
    tenant: Tenant
    actor: User
    staff: User
    client: ClientCompany
    engagement: Engagement
    definition: PlaybookVersion


def cycle_setup(slug: str = "cycle-contract") -> CycleSetup:
    tenant = Tenant.objects.create(
        name="Cycle contract", slug=slug, features=["cycles_r1"]
    )
    actor = User.objects.create_user(email=f"owner@{slug}.test")
    staff = User.objects.create_user(email=f"staff@{slug}.test")
    Membership.objects.create(tenant=tenant, user=actor, role=TenantRole.OWNER)
    Membership.objects.create(
        tenant=tenant, user=staff, role=TenantRole.STAFF_ACCOUNTANT
    )
    with tenant_context(tenant.pk):
        install_system_packs(tenant)
        client = ClientCompany.objects.create(
            tenant=tenant,
            legal_name="Synthetic client",
            cnpj="11222333000181",
            status="active",
        )
        engagement = Engagement.objects.create(
            tenant=tenant,
            client=client,
            service_kind="mei_mensal",
            starts_on=PERIOD,
            responsible=actor,
            provenance="staff_entry",
        )
        TaxProfile.objects.create(
            tenant=tenant,
            client=client,
            regime="mei",
            valid_from=PERIOD,
            provenance="staff_entry",
        )
        definition = PlaybookVersion.objects.get(
            playbook__key="mei-mensal", state="published"
        )
    return CycleSetup(tenant, actor, staff, client, engagement, definition)
