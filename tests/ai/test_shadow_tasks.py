"""The task base must reject missing, inactive and malformed tenant contexts."""

from uuid import uuid4

import pytest

from apps.automation.tasks import classify_document
from apps.core.tenancy import MissingTenantContext
from apps.tenants.models import Tenant

pytestmark = pytest.mark.django_db(transaction=True)


# Explicit ids: the unknown tenant is a fresh UUID, and a random node id would differ
# between pytest-xdist workers, which then refuse to run (AXR1-041).
@pytest.mark.parametrize(
    "tenant_id",
    ["", "invalid", str(uuid4())],
    ids=["empty", "invalid", "unknown-tenant"],
)
def test_invalid_context_fails_closed(tenant_id: str) -> None:
    with pytest.raises(MissingTenantContext):
        classify_document(tenant_id, str(uuid4()))


def test_missing_context_fails_closed() -> None:
    with pytest.raises(MissingTenantContext):
        classify_document()


def test_inactive_context_fails_closed() -> None:
    tenant = Tenant.objects.create(
        name="Disabled", slug="t24-inactive", is_active=False
    )
    with pytest.raises(MissingTenantContext):
        classify_document(str(tenant.pk), str(uuid4()))
