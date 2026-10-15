"""A revocation committed ahead of the authority fence wins over queued work."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from django.db import connection

from apps.core.tenancy import tenant_context
from apps.cycles.bulk import BulkItemState
from apps.cycles.models import ServiceCycle
from apps.cycles.tasks import run_bulk_operation
from apps.tenants.authority import lock_users
from apps.tenants.models import Membership
from tests.cycles.approval_fixtures import LockSignal
from tests.cycles.bulk_fixtures import bulk_setup, confirm, item_states, runtime_call

pytestmark = pytest.mark.django_db(transaction=True)


def test_membership_revocation_commits_before_the_bulk_authority_fence() -> None:
    setup = bulk_setup("bulk-authority-race", count=1)
    operation = confirm(setup.graph.actor, setup.preview())
    entered = Event()

    def execute() -> int:
        with connection.execute_wrapper(LockSignal("accounts_user", entered)):
            return int(run_bulk_operation(str(operation.tenant_id), str(operation.pk)))

    with ThreadPoolExecutor(max_workers=1) as pool:
        with tenant_context(operation.tenant_id):
            lock_users({setup.graph.actor.pk})
            membership = Membership.objects.for_user(setup.graph.actor).get(
                tenant_id=operation.tenant_id,
                user=setup.graph.actor,
                client__isnull=True,
            )
            worker = pool.submit(runtime_call, execute)
            assert entered.wait(15), (
                "The queued worker never reached its authority fence."
            )
            membership.is_active = False
            membership.save(update_fields=["is_active"])
        worker.result(timeout=20)
    with tenant_context(operation.tenant_id):
        assert not ServiceCycle.objects.exists()
        assert set(item_states(operation).values()) == {BulkItemState.SKIPPED_FORBIDDEN}
