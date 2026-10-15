"""Generation eligibility, pinned versions, local dates and honest replay counts."""

from datetime import date

import pytest
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.clients.models import Engagement, TaxProfile
from apps.core.tenancy import tenant_context
from apps.cycles.models import CycleEvent, ServiceCycle
from apps.cycles.services.cycle import open_cycle
from apps.cycles.services.playbooks import create_draft_version, publish_version
from apps.cycles.tasks import generate_cycles_for_period, generate_monthly_cycles
from apps.obligations.models import SchedulerHeartbeat
from tests.cycles.cycle_fixtures import PERIOD, cycle_setup

pytestmark = pytest.mark.django_db(transaction=True)


def test_generation_is_idempotent_and_pins_the_definition_and_tax_profile() -> None:
    setup = cycle_setup()
    assert generate_cycles_for_period(str(setup.tenant.pk), PERIOD.isoformat()) == {
        "created": 1,
        "existing": 0,
    }
    with tenant_context(setup.tenant.pk):
        cycle = ServiceCycle.objects.get()
        assert cycle.tax_profile_id == TaxProfile.objects.get().pk
        original_keys = set(cycle.tasks.values_list("step_key", flat=True))
        # Clone the system pack into a firm definition to exercise the normal
        # version lifecycle separately from the system installer.
        from apps.cycles.services.playbooks import clone_playbook  # noqa: PLC0415

        clone = clone_playbook(
            actor=setup.actor,
            tenant=setup.tenant,
            playbook_pk=setup.definition.playbook_id,
        )
        first, _ = publish_version(
            actor=setup.actor,
            tenant=setup.tenant,
            playbook_pk=clone.pk,
            number=1,
            expected_version=1,
        )
        original = open_cycle(
            setup.actor,
            setup.engagement.pk,
            PERIOD,
            first.pk,
            idempotency_key="original",
        )
        draft = create_draft_version(
            actor=setup.actor, tenant=setup.tenant, playbook_pk=clone.pk
        )
        second, preview = publish_version(
            actor=setup.actor,
            tenant=setup.tenant,
            playbook_pk=clone.pk,
            number=draft.number,
            expected_version=draft.version,
        )
        assert preview.pinned_open_cycles == 1
        replay = open_cycle(
            setup.actor,
            setup.engagement.pk,
            PERIOD,
            second.pk,
            idempotency_key="new-token",
        )
        assert replay.id == original.id
        assert not replay.created
        assert ServiceCycle.objects.get(pk=original.id).playbook_version_id == first.pk
        assert set(cycle.tasks.values_list("step_key", flat=True)) == original_keys
    assert generate_cycles_for_period(str(setup.tenant.pk), PERIOD.isoformat()) == {
        "created": 0,
        "existing": 2,
    }


@pytest.mark.parametrize(
    "case",
    ["inactive", "future", "ended", "wrong_service", "profile_gap", "superseded"],
)
def test_generation_filters_are_load_bearing(case: str) -> None:
    setup = cycle_setup()
    with tenant_context(setup.tenant.pk):
        if case == "inactive":
            setup.client.status = "inactive"
            setup.client.save(update_fields=["status"])
        elif case == "future":
            Engagement.objects.filter(pk=setup.engagement.pk).update(
                starts_on=date(2026, 10, 1)
            )
        elif case == "ended":
            Engagement.objects.filter(pk=setup.engagement.pk).update(
                starts_on=date(2026, 8, 1), ends_on=PERIOD
            )
        elif case == "wrong_service":
            Engagement.objects.filter(pk=setup.engagement.pk).update(
                service_kind="outro"
            )
        elif case == "profile_gap":
            TaxProfile.objects.filter(client=setup.client).update(
                valid_from=date(2026, 10, 1)
            )
        else:
            from django.utils import timezone  # noqa: PLC0415

            TaxProfile.objects.filter(client=setup.client).update(
                superseded_at=timezone.now()
            )
        with pytest.raises(ValidationError):
            open_cycle(
                setup.actor,
                setup.engagement.pk,
                PERIOD,
                setup.definition.pk,
                idempotency_key="ineligible",
            )
        assert not ServiceCycle.objects.exists()
    assert generate_cycles_for_period(str(setup.tenant.pk), PERIOD.isoformat()) == {
        "created": 0,
        "existing": 0,
    }


def test_flag_off_does_not_generate_and_month_day_is_enforced() -> None:
    setup = cycle_setup()
    with tenant_context(setup.tenant.pk):
        with pytest.raises(ValidationError):
            open_cycle(
                setup.actor,
                setup.engagement.pk,
                date(2026, 9, 2),
                setup.definition.pk,
                idempotency_key="invalid",
            )
        ref = open_cycle(
            setup.actor,
            setup.engagement.pk,
            PERIOD,
            setup.definition.pk,
            idempotency_key="valid",
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            ServiceCycle.objects.filter(pk=ref.id).update(period_start=date(2026, 9, 2))
        assert CycleEvent.objects.count() == 1
    setup.tenant.features = []
    setup.tenant.save(update_fields=["features"])
    assert generate_cycles_for_period(str(setup.tenant.pk), "2026-10-01") == {
        "created": 0,
        "existing": 0,
    }


def test_monthly_entrypoint_filters_tenants_and_records_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("apps.cycles.tasks.timezone.localdate", lambda: PERIOD)
    setup = cycle_setup()
    off = cycle_setup("unflagged-cycle")
    off.tenant.features = []
    off.tenant.save(update_fields=["features"])
    result = generate_monthly_cycles()
    assert result == {"created": 1, "existing": 0}
    assert SchedulerHeartbeat.objects.filter(name="generate_cycles").exists()
    with tenant_context(off.tenant.pk):
        assert not ServiceCycle.objects.exists()
    with tenant_context(setup.tenant.pk):
        assert ServiceCycle.objects.count() == 1
    schedule = settings.CELERY_BEAT_SCHEDULE["generate-cycles"]["schedule"]
    assert (schedule.day_of_month, schedule.hour, schedule.minute) == ({1}, {5}, {0})
    assert settings.CELERY_TIMEZONE == "America/Sao_Paulo"
