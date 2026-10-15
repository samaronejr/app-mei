"""External effects require committed DB phases and a validated tenant."""

from uuid import uuid4

import pytest
from celery import current_app
from django.db import connection, transaction

from apps.core.tasks import PhasedTenantTask, assert_no_transaction
from apps.core.tenancy import MissingTenantContext, tenant_context
from apps.core.tests.models import ExampleTenantModel
from apps.tenants.models import Tenant
from tests.support_threads import run_in_thread_as_runtime

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("value", [None, "invalid", 12, uuid4()])
def test_missing_tenant(value: object) -> None:
    with pytest.raises(MissingTenantContext):
        PhasedTenantTask()(value)


def test_inactive_tenant() -> None:
    tenant = Tenant.objects.create(name="Inactive", slug="inactive", is_active=False)
    with pytest.raises(MissingTenantContext):
        PhasedTenantTask()(tenant.pk)


def test_committed_phases_and_fail_closed_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant = Tenant.objects.create(name="Phases", slug="phases")
    PhasedTenantTask.bind(current_app)
    task = PhasedTenantTask()

    def body(tenant_id: str) -> None:
        assert not connection.in_atomic_block
        with tenant_context(tenant.pk):
            ExampleTenantModel.objects.create(tenant=tenant, name="claimed")
        assert_no_transaction()
        assert ExampleTenantModel.objects.count() == 0
        # ALL_OBJECTS_OK: prove the database, not just the manager, fails closed.
        assert ExampleTenantModel.all_objects.count() == 0
        assert (
            run_in_thread_as_runtime(
                tenant.pk, lambda: ExampleTenantModel.objects.get().name
            )
            == "claimed"
        )

    monkeypatch.setattr(task, "run", body)
    task(str(tenant.pk))


def test_outer_transaction_is_refused() -> None:
    with transaction.atomic(), pytest.raises(RuntimeError, match="transaction"):
        assert_no_transaction()
