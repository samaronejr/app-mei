"""Real HTTP conflicts, malformed inputs and portfolio denials never write."""

from datetime import UTC, datetime

import pytest
from django.test import Client
from django.test.utils import override_settings

from apps.audit.models import AuditAction
from apps.audit.services import ObjectRef, record_event
from apps.core.tenancy import tenant_context
from apps.cycles.models import CycleEvent, ServiceCycle
from apps.cycles.services.cycle import open_cycle
from apps.cycles.workspace import timeline
from tests.browser.harness import mfa_session_cookie
from tests.cycles.cycle_fixtures import PERIOD, cycle_setup
from tests.support import enrol_totp

pytestmark = pytest.mark.django_db(transaction=True)


@override_settings(ALLOWED_HOSTS=[".localhost", "localhost", "testserver"])
def test_stale_post_409_and_unassigned_404_zero_writes() -> None:
    setup = cycle_setup()
    with tenant_context(setup.tenant.pk):
        ref = open_cycle(
            setup.actor,
            setup.engagement.pk,
            PERIOD,
            setup.definition.pk,
            idempotency_key="http",
        )
    host = f"{setup.tenant.slug}.localhost"
    owner, staff = Client(SERVER_NAME=host), Client(SERVER_NAME=host)
    for user, session in [(setup.actor, owner), (setup.staff, staff)]:
        enrol_totp(user)
        cookie = mfa_session_cookie(user, f"http://{host}/")
        session.cookies[cookie["name"]] = cookie["value"]
    url = f"/ciclos/{ref.id}/"
    assert owner.get(url).status_code == 200
    data = {"action": "transition", "to_state": "em_andamento", "expected_version": 1}
    assert owner.post(url, data).status_code == 302
    assert owner.post(url, {**data, "to_state": "cancelado"}).status_code == 409
    for path in [url, f"/clientes/{setup.client.pk}/?periodo=2026-09"]:
        assert staff.get(path).status_code == 404
    assert staff.post(url, {**data, "expected_version": 2}).status_code == 404
    denied = staff.get("/ciclos/?periodo=2026-09")
    assert denied.status_code == 200
    assert denied.context["page"].paginator.count == 0
    for path in [
        "/ciclos/?periodo=2026-13",
        "/ciclos/?responsavel=bad",
        f"/clientes/{setup.client.pk}/?periodo=bad",
    ]:
        assert owner.get(path).status_code == 422
    assert (
        owner.post(url, {**data, "expected_version": "not-an-integer"}).status_code
        == 422
    )
    with tenant_context(setup.tenant.pk):
        assert CycleEvent.objects.count() == 2
        assert ServiceCycle.objects.get().state == "em_andamento"
    setup.tenant.features = []
    setup.tenant.save(update_fields=["features"])
    assert owner.get(url).status_code == 404
    assert owner.get("/ciclos/").status_code == 404


def test_timeline_merges_references_only_with_client_and_period_predicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "django.utils.timezone.now", lambda: datetime(2026, 9, 15, tzinfo=UTC)
    )
    setup = cycle_setup()
    with tenant_context(setup.tenant.pk):
        ref = open_cycle(
            setup.actor,
            setup.engagement.pk,
            PERIOD,
            setup.definition.pk,
            idempotency_key="timeline",
        )
        own = record_event(
            action=AuditAction.CLIENT_UPDATED,
            tenant_id=setup.tenant.pk,
            actor=setup.actor,
            obj=ObjectRef(type="clients.ClientCompany", id=str(setup.client.pk)),
            metadata={
                "client_id": str(setup.client.pk),
                "untrusted_text": "not a timeline field",
            },
        )
        other = record_event(
            action=AuditAction.CLIENT_UPDATED,
            tenant_id=setup.tenant.pk,
            actor=setup.actor,
            obj=ObjectRef(type="clients.ClientCompany", id=str(setup.engagement.pk)),
            metadata={"client_id": str(setup.engagement.pk)},
        )
        entries = timeline(setup.actor, setup.client.pk, PERIOD)
        assert {row.id for row in entries} == {
            own.pk,
            CycleEvent.objects.get(cycle_id=ref.id).pk,
        }
        assert other.pk not in {row.id for row in entries}
        assert "untrusted_text" not in str(entries)
        assert timeline(setup.actor, setup.client.pk, PERIOD.replace(month=8)) == []
