"""Every tenant-scoped task must refuse to run tenantless.

Workers run outside `TenantMiddleware`, so `app.tenant_id` is empty and the
fail-closed RLS policies would make a tenantless task silently see zero rows and
report success. `TenantTask.__call__` resolves the tenant before `run` ever
executes; this sweep proves each Axorim task actually inherits that contract by
calling every one of them without a tenant id and requiring
`MissingTenantContext` — or `ValueError` for a syntactically invalid id, which is
the same refusal one layer earlier.

The set is discovered from the Celery registry and asserted against an explicit
list, so a task added without the tenant-aware base fails this file by name rather
than running unscoped on the next deploy.
"""

import importlib
from typing import cast

import pytest
from celery import Task, current_app

from apps.core.tasks import TenantTask
from apps.core.tenancy import MissingTenantContext

pytestmark = pytest.mark.django_db

# The task modules the pivot owns, imported so their @shared_task decorators run
# and the registry is populated. Additions to this list are part of the sweep
# contract: a new TenantTask module that nobody imports here is invisible.
TASK_MODULES = (
    "apps.cycles.tasks",
    "apps.cycles.export_tasks",
    "apps.cycles.retention_tasks",
    "apps.delivery.tasks",
    "apps.automation.tasks",
    "apps.clients.tasks",
    "apps.obligations.tasks",
    "apps.tenants.export_tasks",
)

# The tenant-aware registrations expected at cd19380: (module, name) pairs read
# straight off the decorated callables. A rename, a dropped `base=`, or a new
# task fails this list until the delta is looked at by a human.
# The tenant-aware registrations as of cd19380 — a task bound to TenantTask or
# PhasedTenantTask after this list was written, or removed from it, fails here by
# name. PlatformTask entries are deliberately absent: they hold no tenant
# contract by design, and sweeping them would assert a refusal they never made.
EXPECTED_TENANT_TASKS = frozenset(
    {
        "apps.automation.tasks.classify_document",
        "apps.cycles.export_tasks.build_export",
        "apps.cycles.tasks.extract_document",
        "apps.cycles.tasks.generate_cycles_for_period",
        "apps.cycles.tasks.open_exceptions_for_tenant",
        "apps.cycles.tasks.propagate_document_link",
        "apps.cycles.tasks.run_bulk_operation",
        "apps.cycles.tasks.scan_tenant_quarantine",
        "apps.delivery.tasks.dispatch_tenant",
        "apps.delivery.tasks.sweep_tenant_reminders",
        "apps.tenants.export_tasks.build_account_export",
        "clients.commit_import",
    }
)


def _tenant_tasks() -> dict[str, TenantTask]:
    """Every registered task that runs under the tenant-aware contract.

    Only tasks whose callable lives in an `apps.` module count — the full suite
    also registers TenantTask probes defined inside test modules, and those are
    deliberate one-off fixtures, not application tasks the sweep owns.
    """
    for module in TASK_MODULES:
        importlib.import_module(module)
    current_app.loader.import_default_modules()
    registry = cast("dict[str, Task]", cast("object", current_app.tasks))
    return {
        name: task
        for name, task in registry.items()
        if isinstance(task, TenantTask) and task.__module__.startswith("apps.")
    }


def test_the_tenant_task_registry_matches_the_explicit_list() -> None:
    # Given every registered TenantTask
    found = set(_tenant_tasks())

    # Then it is exactly the expected registrations — a task bound to TenantTask
    # after this list was written, or removed from it, fails here by name.
    assert found == EXPECTED_TENANT_TASKS, (
        f"registry drifted: new={sorted(found - EXPECTED_TENANT_TASKS)} "
        f"gone={sorted(EXPECTED_TENANT_TASKS - found)}"
    )


@pytest.mark.parametrize("name", sorted(EXPECTED_TENANT_TASKS))
def test_a_tenant_task_refuses_to_run_without_a_tenant(name: str) -> None:
    # Given a registered tenant task
    task = _tenant_tasks()[name]

    # When it is invoked with no tenant id — the exact shape a miswired beat or
    # an eager caller produces
    with pytest.raises(MissingTenantContext):
        task()


@pytest.mark.parametrize("name", sorted(EXPECTED_TENANT_TASKS))
def test_a_tenant_task_refuses_a_malformed_tenant_id(name: str) -> None:
    # Given a registered tenant task
    task = _tenant_tasks()[name]

    # When the id is present but not a UUID. TenantTask lets the ValueError
    # surface (fail-closed, unparseable); PhasedTenantTask normalises it to
    # MissingTenantContext. Either is refusal — only a silent run would fail.
    with pytest.raises((MissingTenantContext, ValueError)):
        task("not-a-uuid")


def test_tenantless_tasks_never_reach_run() -> None:
    # Given a probe task on the tenant-aware base
    reached: list[str] = []

    class ProbeTask(TenantTask):
        name = "tests.isolation.probe_tenant_task"

        def run(self, *args: object, **kwargs: object) -> None:
            reached.append("ran")

    task = ProbeTask()

    # When it is called tenantless
    with pytest.raises(MissingTenantContext):
        task()

    # Then `run` never executed — the refusal happens before user code, not after
    # a partial effect.
    assert reached == []
