"""Actual invitation/admin writers serialize with global user activity."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Event

import pytest
from allauth.account.models import EmailAddress
from django.contrib.admin import AdminSite
from django.core.exceptions import ValidationError
from django.db import OperationalError, connection, transaction
from django.forms import modelform_factory
from django.test import RequestFactory
from psycopg.errors import LockNotAvailable

from apps.accounts.invites import InviteScope, accept_invite, issue_invite
from apps.accounts.models import User
from apps.authz.approvals import active_staff_count
from apps.core.tenancy import tenant_context
from apps.tenants.admin import MembershipAdmin
from apps.tenants.models import Invite, Membership, TenantRole
from tests.cycles.approval_fixtures import SIGNAL_TIMEOUT, LockSignal
from tests.cycles.cycle_fixtures import CycleSetup, cycle_setup
from tests.support_threads import run_in_thread_as_runtime

pytestmark = pytest.mark.django_db(transaction=True)


@dataclass(frozen=True, slots=True)
class Roster:
    """One inactive global user and two actual firm rosters."""

    first: CycleSetup
    second: CycleSetup
    member: Membership
    invite: Invite | None


def _roster(operation: str) -> Roster:
    first, second = cycle_setup("roster-first"), cycle_setup("roster-second")
    first.staff.is_active = False
    first.staff.save(update_fields=["is_active"])
    member = Membership.objects.get(
        tenant=first.tenant, user=first.staff, client__isnull=True
    )
    invitation = None
    if operation == "invite":
        EmailAddress.objects.create(
            user=first.staff, email=first.staff.email, verified=True, primary=True
        )
        with tenant_context(second.tenant.pk):
            invitation, _token = issue_invite(
                actor=second.actor,
                tenant=second.tenant,
                email=first.staff.email,
                role=TenantRole.STAFF_ACCOUNTANT,
            )
    first.tenant.refresh_from_db()
    second.tenant.refresh_from_db()
    return Roster(first, second, member, invitation)


def _activate(roster: Roster) -> None:
    with transaction.atomic():
        user = User.objects.select_for_update(no_key=True).get(pk=roster.first.staff.pk)
        user.is_active = True
        user.save(update_fields=["is_active"])


def _admit(roster: Roster, operation: str) -> str:
    if operation == "invite":
        assert roster.invite is not None
        accept_invite(
            invite=roster.invite,
            user=roster.first.staff,
            expected_scope=InviteScope.FIRM,
        )
        return "saved"
    # Exercise the real platform writer's transaction, not a mock save or a claim
    # about admin HTTP authorization. The caller supplies an already-selected row.
    member = Membership.objects.get(pk=roster.member.pk)
    form_class = modelform_factory(
        Membership, fields=["user", "tenant", "client", "role", "is_active"]
    )
    form = form_class(
        {
            "user": str(member.user_id),
            "tenant": str(roster.second.tenant.pk),
            "client": "",
            "role": member.role,
            "is_active": True,
        },
        instance=member,
    )
    assert form.is_valid(), form.errors
    request = RequestFactory().post("/admin/tenants/membership/")
    request.user = roster.second.actor
    writer = MembershipAdmin(Membership, AdminSite())
    try:
        writer.save_model(request, form.save(commit=False), form, change=True)
    except ValidationError:
        return "conflict"
    return "saved"


def _probe_user(roster: Roster) -> None:
    assert User.objects.filter(pk=roster.first.staff.pk).exists()
    with transaction.atomic():
        User.objects.select_for_update(no_key=True, nowait=True).get(
            pk=roster.first.staff.pk
        )


@pytest.mark.parametrize("operation", ["invite", "move"])
@pytest.mark.parametrize("first", ["activity", "roster"])
def test_user_activity_and_real_roster_writers_cover_both_firms_in_both_orders(
    operation: str,
    first: str,
) -> None:
    roster = _roster(operation)
    before = (
        roster.first.tenant.approval_roster_version,
        roster.second.tenant.approval_roster_version,
    )
    entering = Event()

    def contender() -> str:
        with connection.execute_wrapper(LockSignal("accounts_user", entering)):
            if first == "activity":
                return _admit(roster, operation)
            _activate(roster)
            return "activated"

    with ThreadPoolExecutor(max_workers=1) as pool:
        with tenant_context(roster.first.tenant.pk), transaction.atomic():
            if first == "activity":
                _activate(roster)
            else:
                assert _admit(roster, operation) == "saved"
            with pytest.raises(OperationalError) as blocked:
                run_in_thread_as_runtime(
                    roster.first.tenant.pk, lambda: _probe_user(roster)
                )
            assert isinstance(blocked.value.__cause__, LockNotAvailable)
            work = pool.submit(
                run_in_thread_as_runtime, roster.first.tenant.pk, contender
            )
            assert entering.wait(SIGNAL_TIMEOUT)
        outcome = work.result(timeout=SIGNAL_TIMEOUT)
    if operation == "move" and first == "activity":
        assert outcome == "conflict"
        assert (
            Membership.objects.get(pk=roster.member.pk).tenant_id
            == roster.first.tenant.pk
        )
        # A fresh admin submission after the real version conflict is safe.
        with tenant_context(roster.first.tenant.pk):
            assert _admit(roster, operation) == "saved"
    else:
        assert outcome == ("saved" if first == "activity" else "activated")
    roster.first.tenant.refresh_from_db()
    roster.second.tenant.refresh_from_db()
    expected = {
        ("invite", "activity"): (1, 1),
        ("invite", "roster"): (1, 2),
        ("move", "activity"): (2, 1),
        ("move", "roster"): (1, 2),
    }[(operation, first)]
    assert (
        roster.first.tenant.approval_roster_version - before[0],
        roster.second.tenant.approval_roster_version - before[1],
    ) == expected
    assert User.objects.get(pk=roster.first.staff.pk).is_active
    assert active_staff_count(roster.first.tenant.pk) == (
        2 if operation == "invite" else 1
    )
    assert active_staff_count(roster.second.tenant.pk) == 3
