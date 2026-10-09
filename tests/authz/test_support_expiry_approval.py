"""Stored support windows cannot be renewed behind the owner's decision."""

from datetime import datetime, timedelta
from typing import Literal, assert_never

import pytest
from django.db import IntegrityError, connection, transaction
from django.utils import timezone
from psycopg.errors import CheckViolation

from apps.core.tenancy import tenant_context
from apps.tenants.support import SupportApproval, approve_support_grant
from apps.tenants.support_models import SupportGrant, SupportScope
from tests.authz.test_support_grant import _grant_for, _pending, _staff
from tests.tenants.test_cancellation_retention import enabled_firm

pytestmark = pytest.mark.django_db(transaction=True)


def _extend(
    grant: SupportGrant,
    mode: Literal["model", "queryset", "sql"],
    expiry: datetime,
) -> None:
    match mode:
        case "model":
            grant.expires_at = expiry
            grant.save(update_fields=["expires_at"])
        case "queryset":
            SupportGrant.objects.filter(pk=grant.pk).update(expires_at=expiry)
        case "sql":
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE tenants_supportgrant SET expires_at = %s WHERE id = %s",
                    [expiry, grant.pk],
                )
        case unreachable:
            assert_never(unreachable)


@pytest.mark.parametrize("mode", ["model", "queryset", "sql"])
def test_approved_expiry_cannot_be_extended_without_new_owner_approval(
    mode: Literal["model", "queryset", "sql"],
) -> None:
    # Given the owner's approved four-hour window, well below the 72-hour cap.
    firm = enabled_firm("support-expiry-owner")
    grant = _grant_for(firm.tenant, firm.owner, _staff())
    assert grant.expires_at is not None
    original_expiry = grant.expires_at
    extended = original_expiry + timedelta(hours=1)

    # When any persistence path tries to add an hour without another decision.
    with tenant_context(firm.tenant.pk):
        with pytest.raises(IntegrityError) as refused, transaction.atomic():
            _extend(grant, mode, extended)

        # Then the original approved expiry survives, at the database boundary.
        assert isinstance(refused.value.__cause__, CheckViolation)
        assert (
            refused.value.__cause__.diag.constraint_name
            == "support_grant_approved_window_immutable"
        )
        grant.refresh_from_db()
        assert grant.expires_at == original_expiry


def test_pending_request_cannot_set_a_window_without_owner_approval() -> None:
    # Given an undecided request, which admits no access.
    firm = enabled_firm("support-pending-window")
    grant = _pending(firm.tenant, _staff())
    starts = timezone.now()

    # When an unapproved update attempts to set the access window.
    with (
        tenant_context(firm.tenant.pk),
        pytest.raises(IntegrityError) as refused,
        transaction.atomic(),
    ):
        SupportGrant.objects.filter(pk=grant.pk).update(
            starts_at=starts, expires_at=starts + timedelta(hours=1)
        )

    # Then setting expiry requires the complete owner decision.
    assert isinstance(refused.value.__cause__, CheckViolation)
    assert (
        refused.value.__cause__.diag.constraint_name
        == "support_grant_window_requires_owner_approval"
    )


def test_approved_window_cannot_be_reset_for_silent_renewal() -> None:
    # Given a live grant whose original decision must remain attributable.
    firm = enabled_firm("support-no-reset")
    grant = _grant_for(firm.tenant, firm.owner, _staff())

    # When someone tries to clear the decision before rewriting the window.
    with (
        tenant_context(firm.tenant.pk),
        pytest.raises(IntegrityError),
        transaction.atomic(),
    ):
        SupportGrant.objects.filter(pk=grant.pk).update(
            approved_by_owner=None,
            approved_at=None,
            starts_at=None,
            expires_at=None,
        )


def test_another_window_requires_a_new_owner_approved_request() -> None:
    # Given one approved window and another request from the same staff account.
    firm = enabled_firm("support-new-window")
    staff = _staff()
    first = _grant_for(firm.tenant, firm.owner, staff)
    second = _pending(firm.tenant, staff)

    # When the owner explicitly approves a new 72-hour window.
    with tenant_context(firm.tenant.pk):
        approved = approve_support_grant(
            firm.owner,
            firm.tenant.pk,
            second.pk,
            SupportApproval(scope=SupportScope.READ_ONLY, client_ids=(), hours=72),
        )
        first.refresh_from_db()

    # Then the new approval, not a rewritten old row, grants the new expiry.
    assert approved.pk != first.pk
    assert approved.approved_by_owner == firm.owner
    assert approved.expires_at is not None
    assert approved.starts_at is not None
    assert approved.expires_at - approved.starts_at == timedelta(hours=72)
    assert first.expires_at is not None
    assert first.starts_at is not None
    assert first.expires_at - first.starts_at == timedelta(hours=4)
