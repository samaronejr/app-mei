"""Support decisions serialize with the owner's live account authority."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from functools import partial
from threading import Event

import pytest
from django.core.exceptions import PermissionDenied
from django.db import OperationalError, connection, transaction
from psycopg.errors import LockNotAvailable

from apps.accounts.models import User
from apps.accounts.views import _deactivate
from apps.audit.models import AuditAction, PlatformEvent
from apps.audit.models import Event as AuditEvent
from apps.authz.firm_account import ACCOUNT_CAPABILITY, can_manage_account
from apps.authz.models import GrantLevel, RoleGrant
from apps.authz.services import can
from apps.core.tenancy import tenant_context
from apps.tenants.models import Membership, TenantRole
from apps.tenants.support import (
    SupportApproval,
    approve_support_grant,
    revoke_support_grant,
)
from apps.tenants.support_models import SupportGrant, SupportScope
from tests.authz.test_support_grant import _grant_for, _pending, _staff
from tests.cycles.approval_fixtures import SIGNAL_TIMEOUT, LockSignal
from tests.isolation.rolecheck import assert_isolated_role
from tests.support_threads import run_in_thread_as_runtime
from tests.tenants.test_cancellation_retention import enabled_firm
from tests.ui.factories import Firm, add_member

pytestmark = pytest.mark.django_db(transaction=True)


def _decision_for(
    firm: Firm, *, approve: bool
) -> tuple[SupportGrant, Callable[[], SupportGrant]]:
    """Use real requests and decisions, with a nonempty selected-client scope."""
    staff = _staff()
    if approve:
        grant = _pending(firm.tenant, staff)
        return grant, partial(
            approve_support_grant,
            firm.owner,
            firm.tenant.pk,
            grant.pk,
            SupportApproval(
                scope=SupportScope.READ_WRITE_SETTINGS,
                client_ids=(firm.clients[0].pk,),
                hours=2,
            ),
        )
    grant = _grant_for(
        firm.tenant,
        firm.owner,
        staff,
        scope=SupportScope.READ_WRITE_SETTINGS,
        client_ids=(firm.clients[0].pk,),
        hours=2,
    )
    return grant, partial(revoke_support_grant, firm.owner, firm.tenant.pk, grant.pk)


@pytest.mark.parametrize("approve", [True, False], ids=["approve", "revoke"])
@pytest.mark.parametrize(
    "remove_seat", [True, False], ids=["removed-seat", "inactive-user"]
)
def test_authority_revocation_first_denies_support_decision(
    approve: bool, remove_seat: bool
) -> None:
    # Given a decision carrying an owner instance from before authority changes.
    assert_isolated_role()
    firm = enabled_firm(f"support-authority-first-{int(approve)}-{int(remove_seat)}")
    other_owner = add_member(firm.tenant, "other-owner@example.test", TenantRole.OWNER)
    grant, decision = _decision_for(firm, approve=approve)
    action = (
        AuditAction.SUPPORT_GRANT_APPROVED
        if approve
        else AuditAction.SUPPORT_GRANT_REVOKED
    )
    with tenant_context(firm.tenant.pk):
        seat = Membership.objects.get(
            tenant=firm.tenant, user=firm.owner, client__isnull=True
        )
        original = SupportGrant.objects.filter(pk=grant.pk).values().get()
    entering, release = Event(), Event()

    def decide() -> str:
        # The baseline first locks the grant; the repair first locks the user.
        # Gate either exact SQL statement before execution, forwarding it unchanged.
        with (
            connection.execute_wrapper(LockSignal("accounts_user", entering, release)),
            connection.execute_wrapper(
                LockSignal("tenants_supportgrant", entering, release)
            ),
        ):
            try:
                decision()
            except PermissionDenied:
                return "denied"
            return "decided"

    # When authority holds the user fence, then commits before the decision enters it.
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            with tenant_context(firm.tenant.pk):
                if remove_seat:
                    _deactivate(actor=other_owner, membership=seat)
                else:
                    User.objects.filter(pk=firm.owner.pk).update(is_active=False)
                deciding = pool.submit(run_in_thread_as_runtime, firm.tenant.pk, decide)
                assert entering.wait(SIGNAL_TIMEOUT)
            with tenant_context(firm.tenant.pk):
                fresh = User.objects.get(pk=firm.owner.pk)
                assert not can_manage_account(fresh, firm.tenant.pk)
                assert firm.owner.is_active  # Deliberately stale, including activity.
                if remove_seat:
                    seat.refresh_from_db()
                    assert not seat.is_active
                    assert not can(
                        firm.owner, ACCOUNT_CAPABILITY, tenant_id=firm.tenant.pk
                    )
                else:
                    assert not fresh.is_active
        finally:
            release.set()
        outcome = deciding.result(timeout=SIGNAL_TIMEOUT)

    # Then neither the grant row nor either decision audit stream has changed.
    with tenant_context(firm.tenant.pk):
        assert SupportGrant.objects.filter(pk=grant.pk).values().get() == original
        assert not AuditEvent.objects.filter(
            action=action, metadata__grant_id=str(grant.pk)
        ).exists()
        assert not PlatformEvent.objects.filter(
            action=action, tenant_id=firm.tenant.pk, metadata__grant_id=str(grant.pk)
        ).exists()
    assert outcome == "denied"


@pytest.mark.parametrize("approve", [True, False], ids=["approve", "revoke"])
def test_support_decision_first_fences_owner_deactivation(approve: bool) -> None:
    # Given a real decision paused after acquiring the grant and its user fence.
    assert_isolated_role()
    firm = enabled_firm(f"support-decision-first-{int(approve)}")
    other_owner = add_member(firm.tenant, "other-owner@example.test", TenantRole.OWNER)
    grant, decision = _decision_for(firm, approve=approve)
    action = (
        AuditAction.SUPPORT_GRANT_APPROVED
        if approve
        else AuditAction.SUPPORT_GRANT_REVOKED
    )
    with tenant_context(firm.tenant.pk):
        seat = Membership.objects.get(
            tenant=firm.tenant, user=firm.owner, client__isnull=True
        )
    held, release, entering = Event(), Event(), Event()

    def decide() -> SupportGrant:
        with connection.execute_wrapper(
            LockSignal("tenants_supportgrant", held, release, after=True)
        ):
            return decision()

    def probe_user() -> None:
        with transaction.atomic():
            User.objects.select_for_update(no_key=True, nowait=True).get(
                pk=firm.owner.pk
            )

    def remove_owner() -> None:
        with connection.execute_wrapper(LockSignal("accounts_user", entering)):
            _deactivate(actor=other_owner, membership=seat)
        assert AuditEvent.objects.filter(
            action=action, metadata__grant_id=str(grant.pk)
        ).exists()
        assert PlatformEvent.objects.filter(
            action=action, tenant_id=firm.tenant.pk, metadata__grant_id=str(grant.pk)
        ).exists()

    # When removal attempts the shared fence, NOWAIT proves the conflict, not timing.
    with ThreadPoolExecutor(max_workers=2) as pool:
        deciding = pool.submit(run_in_thread_as_runtime, firm.tenant.pk, decide)
        try:
            assert held.wait(SIGNAL_TIMEOUT)
            with pytest.raises(OperationalError) as blocked:
                run_in_thread_as_runtime(firm.tenant.pk, probe_user)
            assert isinstance(blocked.value.__cause__, LockNotAvailable)
            removing = pool.submit(
                run_in_thread_as_runtime, firm.tenant.pk, remove_owner
            )
            assert entering.wait(SIGNAL_TIMEOUT)
        finally:
            release.set()
        deciding.result(timeout=SIGNAL_TIMEOUT)
        removing.result(timeout=SIGNAL_TIMEOUT)

    # Then the decision and both audits commit before the roster revocation.
    with tenant_context(firm.tenant.pk):
        decided = AuditEvent.objects.get(
            action=action, metadata__grant_id=str(grant.pk)
        )
        assert decided.actor == firm.owner
        platform = PlatformEvent.objects.get(
            action=action, tenant_id=firm.tenant.pk, metadata__grant_id=str(grant.pk)
        )
        assert platform.actor == firm.owner
        seat.refresh_from_db()
        assert not seat.is_active
        assert not can_manage_account(firm.owner, firm.tenant.pk)
        grant.refresh_from_db()
        assert grant.is_active() is approve
        assert (grant.revoked_at is None) is approve


@pytest.mark.parametrize("approve", [True, False], ids=["approve", "revoke"])
def test_support_decision_requires_actual_account_capability(approve: bool) -> None:
    # Given an active owner seat whose actual data-driven account grant is NONE.
    assert_isolated_role()
    firm = enabled_firm(f"support-account-capability-{int(approve)}")
    grant, decision = _decision_for(firm, approve=approve)
    action = (
        AuditAction.SUPPORT_GRANT_APPROVED
        if approve
        else AuditAction.SUPPORT_GRANT_REVOKED
    )
    with tenant_context(firm.tenant.pk):
        original = SupportGrant.objects.filter(pk=grant.pk).values().get()
        RoleGrant.objects.filter(
            role=TenantRole.OWNER, capability__slug=ACCOUNT_CAPABILITY
        ).update(level=GrantLevel.NONE)
        assert Membership.objects.get(tenant=firm.tenant, user=firm.owner).is_active
        assert not can(firm.owner, ACCOUNT_CAPABILITY, tenant_id=firm.tenant.pk)

        # When the service is reached directly, the view's gate cannot authorize it.
        with pytest.raises(PermissionDenied):
            decision()

        # Then no decision row change or decision audit is committed.
        assert SupportGrant.objects.filter(pk=grant.pk).values().get() == original
        assert not AuditEvent.objects.filter(
            action=action, metadata__grant_id=str(grant.pk)
        ).exists()
        assert not PlatformEvent.objects.filter(
            action=action, tenant_id=firm.tenant.pk, metadata__grant_id=str(grant.pk)
        ).exists()


@pytest.mark.parametrize("approve", [True, False], ids=["approve", "revoke"])
def test_current_owner_support_decision_writes_both_audits(approve: bool) -> None:
    # Given an ordinary active owner and a grant in the appropriate decision state.
    assert_isolated_role()
    firm = enabled_firm(f"support-current-owner-{int(approve)}")
    grant, decision = _decision_for(firm, approve=approve)
    action = (
        AuditAction.SUPPORT_GRANT_APPROVED
        if approve
        else AuditAction.SUPPORT_GRANT_REVOKED
    )

    # When the current owner makes the authorized decision.
    with tenant_context(firm.tenant.pk):
        assert can_manage_account(firm.owner, firm.tenant.pk)
        decision()
        grant.refresh_from_db()

        # Then scope, owner and window stay bound, and both audits name this decision.
        assert grant.approved_by_owner == firm.owner
        assert grant.scope == SupportScope.READ_WRITE_SETTINGS
        assert grant.client_ids == [str(firm.clients[0].pk)]
        assert grant.starts_at is not None
        assert grant.expires_at is not None
        assert grant.expires_at - grant.starts_at == timedelta(hours=2)
        assert grant.is_active() is approve
        assert (grant.revoked_at is None) is approve
        tenant_event = AuditEvent.objects.get(
            action=action, metadata__grant_id=str(grant.pk)
        )
        platform_event = PlatformEvent.objects.get(
            action=action, tenant_id=firm.tenant.pk, metadata__grant_id=str(grant.pk)
        )
        assert tenant_event.actor == firm.owner
        assert platform_event.actor == firm.owner
