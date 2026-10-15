"""Real isolated tenant/cycle graph, independent storage and a bounded SMTP peer."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID

import pytest
from django.test import override_settings
from pytest_django.fixtures import SettingsWrapper

from apps.clients.models import ClientContact
from apps.core.tenancy import tenant_context
from apps.cycles.models import ServiceCycle
from apps.cycles.services.cycle import open_cycle
from apps.delivery.models import OutboundMessage
from apps.delivery.outbox import enqueue
from apps.delivery.policy import BusinessIntent
from tests.cycles.cycle_fixtures import PERIOD, CycleSetup, cycle_setup
from tests.delivery.smtp import FakeSMTP


@dataclass
class DeliverySetup:
    graph: CycleSetup
    cycle: ServiceCycle
    contact: ClientContact
    intent: BusinessIntent

    def queue(
        self,
        *,
        occurrence: int = 0,
        kind: str = "notice",
        contact_id: UUID | str | None = None,
    ) -> OutboundMessage:
        with tenant_context(self.graph.tenant.pk):
            self.graph.client.refresh_from_db()
            return enqueue(
                self.graph.actor,
                self.graph.client.pk,
                contact_id or self.contact.pk,
                intent=replace(self.intent, occurrence=occurrence),
                kind=kind,
                expected_version=self.graph.client.version,
                cycle_id=self.cycle.pk,
            )

    def reload(self, message: OutboundMessage) -> OutboundMessage:
        with tenant_context(self.graph.tenant.pk):
            message.refresh_from_db()
        return message


@pytest.fixture
def delivery(settings: SettingsWrapper, tmp_path: Path) -> DeliverySetup:
    settings.DELIVERY_PAUSED = False
    settings.MEDIA_ROOT = tmp_path
    graph = cycle_setup("delivery")
    with tenant_context(graph.tenant.pk):
        ref = open_cycle(
            graph.actor,
            graph.engagement.pk,
            PERIOD,
            graph.definition.pk,
            idempotency_key="fixture",
        )
        cycle = ServiceCycle.objects.get(pk=ref.id)
        contact = ClientContact.objects.create(
            tenant=graph.tenant,
            client=graph.client,
            name="Synthetic contact",
            email="contact@example.test",
        )
    return DeliverySetup(
        graph,
        cycle,
        contact,
        BusinessIntent(
            graph.engagement.pk,
            graph.definition.playbook_id,
            cycle.period_start,
            cycle.period_end,
            cycle.amendment,
            "notice",
            "email_notice",
        ),
    )


@pytest.fixture
def smtp_server() -> Iterator[Callable[[str], FakeSMTP]]:
    servers: list[FakeSMTP] = []
    overrides: list[override_settings] = []

    def start(mode: str = "accept") -> FakeSMTP:
        server = FakeSMTP(mode=mode).__enter__()
        servers.append(server)
        override = override_settings(
            EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
            EMAIL_HOST="127.0.0.1",
            EMAIL_PORT=server.port,
            EMAIL_HOST_USER="",
            EMAIL_HOST_PASSWORD="",
            EMAIL_USE_TLS=False,
            EMAIL_USE_SSL=False,
            EMAIL_TIMEOUT=3,
        )
        override.enable()
        overrides.append(override)
        return server

    yield start
    for override in reversed(overrides):
        override.disable()
    for server in reversed(servers):
        server.__exit__(None, None, None)
