"""Real tenant graphs and runtime-role threads for the bulk contract."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from django.db import connection, connections
from django.utils import timezone

from apps.accounts.models import User
from apps.clients.models import ClientCompany, Engagement, TaxProfile
from apps.core.tenancy import tenant_context
from apps.cycles.bulk import BulkKind, BulkOperation
from apps.cycles.bulk_contracts import PreviewExpectation, Selection
from apps.cycles.models import ServiceCycle
from apps.cycles.services.bulk import preview_bulk, run_bulk
from apps.cycles.services.cycle import open_cycle
from apps.cycles.services.requests import RequestFields, create_request
from apps.delivery.models import AutomationPause
from tests.cycles.cycle_fixtures import PERIOD, CycleSetup, cycle_setup
from tests.portal.request_fixtures import PortalSetup, portal_setup
from tests.ui.factories import make_cnpj


@dataclass(frozen=True, slots=True)
class BulkSetup:
    graph: CycleSetup
    engagements: tuple[Engagement, ...]

    def preview(self) -> BulkOperation:
        with tenant_context(self.graph.tenant.pk):
            return preview_bulk(
                self.graph.actor,
                BulkKind.OPEN_CYCLES,
                Selection(
                    tuple(row.pk for row in self.engagements),
                    period=PERIOD,
                    playbook_version_id=self.graph.definition.pk,
                ),
            )

    def cycles(self) -> tuple[ServiceCycle, ...]:
        with tenant_context(self.graph.tenant.pk):
            return tuple(
                ServiceCycle.objects.get(
                    pk=open_cycle(
                        self.graph.actor,
                        engagement.pk,
                        PERIOD,
                        self.graph.definition.pk,
                        idempotency_key=f"fixture:{engagement.pk}",
                    ).id
                )
                for engagement in self.engagements
            )


def bulk_setup(slug: str = "bulk-contract", *, count: int = 3) -> BulkSetup:
    graph = cycle_setup(slug)
    graph.tenant.features = ["cycles_r1", "bulk_r1"]
    graph.tenant.save(update_fields=["features"])
    engagements = [graph.engagement]
    with tenant_context(graph.tenant.pk):
        for index in range(1, count):
            client = ClientCompany.objects.create(
                tenant=graph.tenant,
                legal_name=f"Cliente sintético {index + 1}",
                cnpj=make_cnpj(f"178900000{index:03d}"),
                status="active",
            )
            engagements.append(
                Engagement.objects.create(
                    tenant=graph.tenant,
                    client=client,
                    service_kind="mei_mensal",
                    starts_on=PERIOD - timedelta(days=31),
                    responsible=graph.actor,
                    provenance="staff_entry",
                )
            )
            TaxProfile.objects.create(
                tenant=graph.tenant,
                client=client,
                regime="mei",
                valid_from=PERIOD,
                provenance="staff_entry",
            )
    return BulkSetup(graph, tuple(engagements))


def notice_preview_with_reservations(
    slug: str, *, reserved: int
) -> tuple[PortalSetup, BulkOperation]:
    """Preview one draft request notice after `reserved` of today's sends."""
    setup = portal_setup(slug)
    graph = setup.graph
    graph.tenant.features = ["cycles_r1", "bulk_r1"]
    graph.tenant.save(update_fields=["features"])
    with tenant_context(graph.tenant.pk):
        create_request(
            graph.actor,
            setup.cycle.pk,
            RequestFields(title="Extrato adicional"),
            expected_version=setup.cycle.version,
        )
        AutomationPause.objects.update_or_create(
            tenant=graph.tenant,
            defaults={"quota_day": timezone.localdate(), "reserved_today": reserved},
        )
        preview = preview_bulk(
            graph.actor, BulkKind.SEND_REQUEST_NOTICES, Selection((setup.cycle.pk,))
        )
    return setup, preview


def confirm(
    actor: User,
    preview: BulkOperation,
    *,
    exclusions: tuple[str, ...] = (),
) -> BulkOperation:
    """Confirm exactly the eligible, nonexcluded snapshot entries."""
    count = sum(
        row["eligible"] and row["subject_ref"] not in exclusions
        for row in preview.snapshot_json["items"]
    )
    with tenant_context(preview.tenant_id):
        return run_bulk(
            actor,
            preview.pk,
            expectation=PreviewExpectation(preview.version, preview.snapshot_digest),
            confirm_count=count,
            exclusions=exclusions,
        )


def runtime_call[T](function: Callable[[], T]) -> T:
    """Use a fresh real-role connection without wrapping a phased task."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE app_runtime")
            cursor.execute("SET statement_timeout = '15s'")
            cursor.execute("SELECT current_user, current_setting('row_security')")
            assert cursor.fetchone() == ("app_runtime", "on")
        assert not connection.in_atomic_block
        return function()
    finally:
        connections.close_all()


def item_states(operation: BulkOperation) -> dict[UUID, str]:
    with tenant_context(operation.tenant_id):
        return {
            UUID(row.subject_ref): row.state
            for row in operation.items.order_by("position")
        }
