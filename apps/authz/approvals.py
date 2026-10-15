"""Fresh firm authority for exact-effect decisions; no support impersonation."""

from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from apps.accounts.models import User
from apps.authz.models import GrantLevel, RoleGrant, UserCapabilityGrant
from apps.authz.portfolio import (
    VIEW_ALL,
    VIEW_ASSIGNED,
    PortfolioScope,
    portfolio_scope,
    visible_clients,
)
from apps.authz.services import can, granted_levels_with_role, role_of
from apps.clients.models import ClientAssignment
from apps.cycles.effects.manifest import (
    AuthorizationSnapshot,
    GrantSnapshot,
    JSONValue,
    canonicalize,
    utc_timestamp,
)
from apps.tenants.authority import lock_users
from apps.tenants.models import FIRM_ROLES, Membership, TenantRole

APPROVE: Final = "actions.approve"
AUTHORITY_CAPABILITIES: Final = (
    "cycles.operate",
    "cycles.complete",
    "cycles.review",
    APPROVE,
    # Bulk approval composes this same fence; its final can() stays locked too.
    "bulk.run",
    VIEW_ALL,
    VIEW_ASSIGNED,
)


@dataclass(frozen=True, slots=True)
class ProposalControls:
    """Presentation hints only; decision commands repeat live authorization."""

    approve: bool
    propose: bool
    cancel_others: bool


def proposal_controls(
    user: User, tenant_id: UUID, initiation_capability: str
) -> ProposalControls:
    """Resolve this screen's controls once, independently of portfolio/navigation."""
    role, levels = granted_levels_with_role(
        user, (APPROVE, initiation_capability), tenant_id=tenant_id
    )
    ordinary = user.is_active and not user.is_superuser
    return ProposalControls(
        approve=ordinary and levels[APPROVE] == GrantLevel.FULL,
        propose=ordinary and levels[initiation_capability] == GrantLevel.FULL,
        cancel_others=ordinary and role == TenantRole.OWNER,
    )


@dataclass(frozen=True, slots=True)
class Authority:
    """Rows locked before the policy and client fences, never a cached answer."""

    user: User
    membership: Membership | None
    role_grants: tuple[RoleGrant, ...]
    user_grants: tuple[UserCapabilityGrant, ...]

    def permits(self, capability: str, client_id: UUID, tenant_id: UUID) -> bool:
        """Re-run the normal matrix and portfolio rules on the fresh identity."""
        return bool(
            self.membership is not None
            and self.membership.is_active
            and self.membership.role in FIRM_ROLES
            and self.user.is_active
            and not self.user.is_superuser
            and can(self.user, capability, tenant_id=tenant_id)
            and visible_clients(self.user, tenant_id=tenant_id)
            .filter(pk=client_id)
            .exists()
        )

    def snapshot(
        self, capability: str, *, client_id: UUID, now: datetime
    ) -> AuthorizationSnapshot:
        """Bind real row ids/revisions and grant expiry, including portfolio policy."""
        if self.membership is None:
            msg = "An authority snapshot requires a firm membership."
            raise ValueError(msg)
        slugs = {capability, VIEW_ALL, VIEW_ASSIGNED}
        grants: list[GrantSnapshot] = [
            {"id": str(row.pk), "version": row.version, "expires_at": None}
            for row in self.role_grants
            if row.capability.slug in slugs
        ]
        grants.extend(
            {
                "id": str(row.pk),
                "version": row.version,
                "expires_at": row.expires_at,
            }
            for row in self.user_grants
            if row.capability.slug in slugs
            and row.revoked_at is None
            and (row.expires_at is None or row.expires_at > now)
        )
        assignment_id = None
        if (
            portfolio_scope(self.user, tenant_id=self.membership.tenant_id)
            is PortfolioScope.ASSIGNED
        ):
            assignment_id = (
                ClientAssignment.objects.filter(client_id=client_id, user=self.user)
                .values_list("pk", flat=True)
                .first()
            )
        return {
            "membership_id": str(self.membership.pk),
            "membership_version": self.membership.version,
            "portfolio_assignment_id": (
                str(assignment_id) if assignment_id is not None else None
            ),
            "grants": tuple(grants),
        }

    def snapshot_data(
        self, capability: str, *, client_id: UUID, now: datetime
    ) -> dict[str, JSONValue]:
        """Use the manifest's same canonical grant representation for a decision."""
        snapshot = self.snapshot(capability, client_id=client_id, now=now)
        grants: list[JSONValue] = [
            {
                "id": row["id"],
                "version": row["version"],
                "expires_at": (
                    utc_timestamp(row["expires_at"])
                    if row["expires_at"] is not None
                    else None
                ),
            }
            for row in snapshot["grants"]
        ]
        return {
            "membership_id": snapshot["membership_id"],
            "membership_version": snapshot["membership_version"],
            "portfolio_assignment_id": snapshot["portfolio_assignment_id"],
            "grants": sorted(grants, key=canonicalize),
        }


def lock_authorities(
    user_ids: set[UUID],
    *,
    tenant_id: UUID,
    extra_capabilities: tuple[str, ...] = (),
) -> dict[UUID, Authority]:
    """Acquire users, seats and actual policy rows in one deterministic order."""
    capabilities = (*AUTHORITY_CAPABILITIES, *extra_capabilities)
    users = lock_users(user_ids)
    members = {
        row.user_id: row
        # PLATFORM_QUERY_OK: these exact participants and this tenant define the
        # authorization question; using for_user here would ask it recursively.
        for row in Membership.objects.select_for_update(no_key=True)
        .filter(tenant_id=tenant_id, client__isnull=True, user_id__in=user_ids)
        .order_by("pk")
    }
    role_rows = tuple(
        RoleGrant.objects.select_for_update(no_key=True, of=("self",))
        .select_related("capability")
        .filter(
            role__in={member.role for member in members.values()},
            capability__slug__in=capabilities,
        )
        .order_by("pk")
    )
    user_rows = tuple(
        UserCapabilityGrant.objects.select_for_update(no_key=True, of=("self",))
        .select_related("capability")
        .filter(user_id__in=user_ids, capability__slug__in=capabilities)
        .order_by("pk")
    )
    return {
        user_id: Authority(
            user=user,
            membership=members.get(user_id),
            role_grants=tuple(
                row
                for row in role_rows
                if user_id in members and row.role == members[user_id].role
            ),
            user_grants=tuple(row for row in user_rows if row.user_id == user_id),
        )
        for user_id, user in users.items()
    }


def active_staff_count(tenant_id: UUID) -> int:
    """Count actual active firm seats after the tenant admission fence is held."""
    # PLATFORM_QUERY_OK: one explicit firm's live staff count is policy input,
    # not a list of people exposed to a caller.
    return Membership.objects.filter(
        tenant_id=tenant_id,
        client__isnull=True,
        role__in=FIRM_ROLES,
        is_active=True,
        user__is_active=True,
    ).count()


def is_policy_owner(authority: Authority) -> bool:
    """Only an ordinary active firm owner may change self-approval policy."""
    member = authority.membership
    return bool(
        member is not None
        and member.is_active
        and member.role == TenantRole.OWNER
        and authority.user.is_active
        and not authority.user.is_superuser
    )


def may_configure_policy(user: User, tenant_id: UUID) -> bool:
    """Offer the owner form; its command repeats this under the authority locks."""
    return bool(
        user.is_active
        and not user.is_superuser
        and role_of(user, tenant_id) == TenantRole.OWNER
    )
