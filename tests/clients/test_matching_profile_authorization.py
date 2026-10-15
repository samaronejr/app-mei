"""An unchanged claim is not an authorization or concurrency bypass."""

from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied
from django.http import Http404

from apps.accounts.models import User
from apps.audit.models import Event
from apps.clients.models import ClientCompany, TaxProfile
from apps.clients.services import TaxProfileConflictError, sync_profile_flag
from apps.core.tenancy import tenant_context
from apps.tenants.models import Tenant
from tests.clients import test_sync_profile_flag as sync_tests
from tests.clients.test_sync_profile_flag import _client

pytestmark = pytest.mark.django_db(transaction=True)
firm = sync_tests.firm


def test_matching_profile_still_checks_scope_capability_and_revision(
    firm: tuple[Tenant, User, User],
) -> None:
    tenant, owner, staff = firm
    client = _client(tenant, is_mei=True)
    with tenant_context(tenant.id):
        sync_profile_flag(actor=owner, client=client, created=True)
        client.refresh_from_db()
        before = (
            list(ClientCompany.objects.values()),
            list(TaxProfile.objects.values()),
            list(Event.objects.values()),
        )
        with pytest.raises(Http404):
            sync_profile_flag(actor=staff, client=client, created=False)
        with (
            patch("apps.clients.services.can", return_value=False),
            pytest.raises(PermissionDenied),
        ):
            sync_profile_flag(actor=owner, client=client, created=False)
        client.tax_profile_revision -= 1
        with pytest.raises(TaxProfileConflictError):
            sync_profile_flag(actor=owner, client=client, created=False)
        assert (
            list(ClientCompany.objects.values()),
            list(TaxProfile.objects.values()),
            list(Event.objects.values()),
        ) == before
