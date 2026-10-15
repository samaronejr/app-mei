"""A settings write and owner revocation have a single database order."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from django.core.exceptions import PermissionDenied
from django.db import OperationalError, connection, transaction
from django.http import Http404
from psycopg.errors import LockNotAvailable

from apps.accounts.models import User
from apps.audit.models import AuditAction
from apps.audit.models import Event as AuditEvent
from apps.authz.support import SupportContext, current_support
from apps.core.tenancy import tenant_context
from apps.tenants.services import FirmProfile, save_profile
from apps.tenants.support import revoke_support_grant
from apps.tenants.support_models import SupportGrant, SupportScope
from tests.authz.test_support_grant import _grant_for, _staff
from tests.cycles.approval_fixtures import SIGNAL_TIMEOUT, LockSignal
from tests.support_threads import run_in_thread_as_runtime
from tests.tenants.test_cancellation_retention import enabled_firm
from tests.ui.factories import Firm

pytestmark = pytest.mark.django_db(transaction=True)


def _save_as_support(firm: Firm, staff: User, grant: SupportGrant) -> None:
    binding = current_support.set(
        SupportContext(
            grant_id=grant.pk,
            tenant_id=firm.tenant.pk,
            scope=grant.scope,
            client_ids=frozenset(grant.client_uuid_set()),
            expires_at=grant.expires_at,
        )
    )
    try:
        save_profile(
            staff,
            firm.tenant.pk,
            FirmProfile(name="Saved before revocation"),
            expected_version=firm.tenant.settings_version,
        )
    finally:
        current_support.reset(binding)


def test_settings_write_first_fences_owner_revocation() -> None:
    # Given a live settings grant and a writer held at its real tenant lock.
    firm = enabled_firm("support-write-first")
    staff = _staff()
    grant = _grant_for(
        firm.tenant, firm.owner, staff, scope=SupportScope.READ_WRITE_SETTINGS
    )
    held, release, entering = Event(), Event(), Event()

    def write() -> None:
        with connection.execute_wrapper(
            LockSignal("tenants_tenant", held, release, after=True)
        ):
            _save_as_support(firm, staff, grant)

    def probe_grant() -> None:
        with transaction.atomic():
            SupportGrant.objects.select_for_update(no_key=True, nowait=True).get(
                pk=grant.pk
            )

    def revoke() -> None:
        with connection.execute_wrapper(LockSignal("tenants_supportgrant", entering)):
            revoke_support_grant(firm.owner, firm.tenant.pk, grant.pk)

    # When the owner races an in-flight settings write.
    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(run_in_thread_as_runtime, firm.tenant.pk, write)
        try:
            assert held.wait(SIGNAL_TIMEOUT)
            # NOWAIT proves the fence, without judging a negative timing window.
            with pytest.raises(OperationalError) as blocked:
                run_in_thread_as_runtime(firm.tenant.pk, probe_grant)
            assert isinstance(blocked.value.__cause__, LockNotAvailable)
            revoking = pool.submit(run_in_thread_as_runtime, firm.tenant.pk, revoke)
            assert entering.wait(SIGNAL_TIMEOUT)
        finally:
            release.set()
        writing.result(timeout=SIGNAL_TIMEOUT)
        revoking.result(timeout=SIGNAL_TIMEOUT)

    # Then the write is ordered before revocation, and the next write is refused.
    with tenant_context(firm.tenant.pk):
        written = AuditEvent.objects.get(action="tenant.profile_updated")
        revoked = AuditEvent.objects.get(action=AuditAction.SUPPORT_GRANT_REVOKED)
        assert written.created_at <= revoked.created_at
        firm.tenant.refresh_from_db()
        assert firm.tenant.name == "Saved before revocation"
        with pytest.raises(Http404):
            _save_as_support(firm, staff, grant)


def test_owner_revocation_first_refuses_the_waiting_settings_write() -> None:
    # Given a write carrying the admitted request's stale grant snapshot.
    firm = enabled_firm("support-revoke-first")
    staff = _staff()
    grant = _grant_for(
        firm.tenant, firm.owner, staff, scope=SupportScope.READ_WRITE_SETTINGS
    )
    original_name = firm.tenant.name
    entering, release = Event(), Event()

    def write() -> str:
        with connection.execute_wrapper(LockSignal("accounts_user", entering, release)):
            try:
                _save_as_support(firm, staff, grant)
            except (Http404, PermissionDenied):
                return "denied"
            return "saved"

    # When the owner commits revocation before the writer crosses its user fence.
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            with tenant_context(firm.tenant.pk):
                revoke_support_grant(firm.owner, firm.tenant.pk, grant.pk)
                writing = pool.submit(run_in_thread_as_runtime, firm.tenant.pk, write)
                assert entering.wait(SIGNAL_TIMEOUT)
        finally:
            release.set()
        assert writing.result(timeout=SIGNAL_TIMEOUT) == "denied"

    # Then no settings mutation or settings event was committed.
    with tenant_context(firm.tenant.pk):
        firm.tenant.refresh_from_db()
        assert firm.tenant.name == original_name
        assert not AuditEvent.objects.filter(action="tenant.profile_updated").exists()
