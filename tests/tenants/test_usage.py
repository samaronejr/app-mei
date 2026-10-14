"""Immutable metering counts real units once and keeps owner estimates private."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, IntegrityError, transaction
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.tenants.models import (
    FirmMessageKind,
    FirmMessageState,
    FirmOutboundMessage,
    UsageKind,
    UsageMetric,
    UsageRecord,
)
from apps.tenants.tasks import record_usage
from apps.tenants.usage import cost_estimate, record_tenant_usage
from tests.tenants.test_cancellation_retention import enabled_firm
from tests.ui.factories import make_firm

pytestmark = pytest.mark.django_db(transaction=True)
TODAY = date(2026, 10, 8)


def test_nightly_replay_is_idempotent_and_only_enabled_firms_are_measured() -> None:
    enabled = enabled_firm("usage-enabled")
    dark = make_firm("usage-dark")
    assert record_usage(on=TODAY) == 3
    assert record_usage(on=TODAY) == 0
    with tenant_context(enabled.tenant.pk):
        assert UsageRecord.objects.count() == 3
        assert UsageRecord.objects.get(metric=UsageMetric.ACTIVE_CLIENTS).quantity == 1
        assert UsageRecord.objects.get(metric=UsageMetric.STORAGE_BYTES).kind == (
            UsageKind.ESTIMATED
        )
        assert not UsageRecord.objects.filter(
            metric=UsageMetric.AI_SHADOW_CALLS
        ).exists()
    with tenant_context(dark.tenant.pk):
        assert not UsageRecord.objects.exists()


def test_email_flow_covers_the_complete_prior_day_not_a_partial_current_day() -> None:
    firm = enabled_firm("usage-email-window")
    yesterday = TODAY - timedelta(days=1)
    with tenant_context(firm.tenant.pk):
        for index, day in enumerate((yesterday, TODAY)):
            FirmOutboundMessage.objects.create(
                tenant=firm.tenant,
                user=firm.owner,
                kind=FirmMessageKind.DAILY_SUMMARY,
                period=day,
                state=FirmMessageState.ACCEPTED,
                idempotency_key=f"{index:064d}",
                finished_at=datetime.combine(day, datetime.min.time(), tzinfo=UTC)
                + timedelta(hours=20),
            )
    record_tenant_usage(firm.tenant.pk, on=TODAY)
    record_tenant_usage(firm.tenant.pk, on=TODAY + timedelta(days=1))
    with tenant_context(firm.tenant.pk):
        measured = list(
            UsageRecord.objects.filter(metric=UsageMetric.EMAILS_SENT)
            .order_by("period")
            .values_list("period", "quantity")
        )
        assert measured == [(yesterday, 1), (TODAY, 1)]


def test_usage_is_append_only_and_requires_an_idempotent_source() -> None:
    firm = enabled_firm("usage-immutable")
    source = uuid4()
    with tenant_context(firm.tenant.pk):
        record = UsageRecord.objects.create(
            tenant=firm.tenant,
            metric=UsageMetric.AI_SHADOW_CALLS,
            period=TODAY,
            quantity=1,
            source_event=source,
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            UsageRecord.objects.create(
                tenant=firm.tenant,
                metric=record.metric,
                period=TODAY,
                quantity=1,
                source_event=source,
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            UsageRecord.objects.filter(pk=record.pk).update(quantity=2)
        with pytest.raises(DatabaseError), transaction.atomic():
            UsageRecord.objects.filter(pk=record.pk).delete()
        record.refresh_from_db()
        assert record.quantity == 1


def test_paired_corrections_cannot_change_the_metric_or_tenant() -> None:
    firm = enabled_firm("usage-correction")
    other = enabled_firm("usage-correction-other")
    with tenant_context(firm.tenant.pk):
        original = UsageRecord.objects.create(
            tenant=firm.tenant,
            metric=UsageMetric.AI_SHADOW_CALLS,
            period=TODAY,
            quantity=1,
            source_event=uuid4(),
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            UsageRecord.objects.create(
                tenant=firm.tenant,
                metric=UsageMetric.EMAILS_SENT,
                period=TODAY,
                quantity=2,
                source_event=uuid4(),
                correction_of=original,
            )
    with (
        tenant_context(other.tenant.pk),
        pytest.raises(IntegrityError),
        transaction.atomic(),
    ):
        UsageRecord.objects.create(
            tenant=other.tenant,
            metric=original.metric,
            period=TODAY,
            quantity=2,
            source_event=uuid4(),
            correction_of_id=original.pk,
        )


def test_owner_estimate_excludes_corrected_readings_and_never_invents_prices(
    settings: SettingsWrapper,
) -> None:
    firm = enabled_firm("usage-estimate")
    record_tenant_usage(firm.tenant.pk, on=TODAY)
    with tenant_context(firm.tenant.pk):
        original = UsageRecord.objects.create(
            tenant=firm.tenant,
            metric=UsageMetric.AI_SHADOW_CALLS,
            period=TODAY,
            quantity=9,
            source_event=uuid4(),
        )
        UsageRecord.objects.create(
            tenant=firm.tenant,
            metric=original.metric,
            period=TODAY,
            quantity=2,
            source_event=uuid4(),
            correction_of=original,
        )
        missing = cost_estimate(firm.owner, firm.tenant.pk, on=TODAY)
        assert not missing.complete
        assert missing.total is None
        settings.USAGE_UNIT_PRICES_BRL = {
            UsageMetric.ACTIVE_CLIENTS: "10",
            UsageMetric.STORAGE_BYTES: "0.000000001",
            UsageMetric.EMAILS_SENT: "0.01",
            UsageMetric.AI_SHADOW_CALLS: "1",
        }
        measured = cost_estimate(firm.owner, firm.tenant.pk, on=TODAY)
        assert not measured.complete
        assert measured.total is None
        assert sum(
            line.cost for line in measured.lines if line.cost is not None
        ) == Decimal(12)
        with pytest.raises(PermissionDenied):
            cost_estimate(firm.accountant, firm.tenant.pk, on=TODAY)


@pytest.mark.parametrize(
    ("days", "accepted_today", "partial"),
    [
        ((), False, True),
        ((), True, True),
        ((7,), False, True),
        ((7,), True, True),
        ((6,), False, True),
        ((1, 8), False, True),
        (tuple(range(1, 9)), False, False),
    ],
)
def test_email_estimate_distinguishes_missing_partial_and_measured_zero(
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    days: tuple[int, ...],
    accepted_today: bool,
    partial: bool,
) -> None:
    firm = enabled_firm("email-coverage")
    settings.USAGE_UNIT_PRICES_BRL = dict.fromkeys(UsageMetric, "1")
    monkeypatch.setattr("apps.tenants.account_views.timezone.localdate", lambda: TODAY)
    with tenant_context(firm.tenant.pk):
        for day in days:
            UsageRecord.objects.create(
                tenant=firm.tenant,
                metric=UsageMetric.EMAILS_SENT,
                period=TODAY.replace(day=day),
                quantity=0,
                source_event=uuid4(),
            )
        for metric in (UsageMetric.ACTIVE_CLIENTS, UsageMetric.STORAGE_BYTES):
            UsageRecord.objects.create(
                tenant=firm.tenant,
                metric=metric,
                period=TODAY,
                quantity=0,
                source_event=uuid4(),
            )
        if accepted_today:
            FirmOutboundMessage.objects.create(
                tenant=firm.tenant,
                user=firm.owner,
                kind=FirmMessageKind.DAILY_SUMMARY,
                period=TODAY,
                state=FirmMessageState.ACCEPTED,
                idempotency_key="a" * 64,
                finished_at=datetime(2026, 10, 8, 15, tzinfo=UTC),
            )
    response = firm.as_owner().get(reverse("firm-account"))
    assert response.status_code == 200
    estimate = response.context["estimate"]
    email = next(
        line for line in estimate.lines if line.metric == UsageMetric.EMAILS_SENT
    )
    assert email.quantity == (0 if days else None)
    assert email.cost == (Decimal(0) if days else None)
    assert estimate.complete is (not partial)
    assert estimate.total == (None if partial else Decimal(0))
    assert email.covered_through == (TODAY.replace(day=max(days)) if days else None)
    assert email.measured_days == len(days)
    assert email.partial is partial


def test_email_coverage_uses_corrected_readings_and_month_boundary() -> None:
    firm = enabled_firm("email-corrected-coverage")
    record_tenant_usage(firm.tenant.pk, on=TODAY.replace(day=1))
    with tenant_context(firm.tenant.pk):
        missing = cost_estimate(firm.owner, firm.tenant.pk, on=TODAY)
        email = next(
            line for line in missing.lines if line.metric == UsageMetric.EMAILS_SENT
        )
        assert email.quantity is None
        original = UsageRecord.objects.create(
            tenant=firm.tenant,
            metric=UsageMetric.EMAILS_SENT,
            period=TODAY - timedelta(days=1),
            quantity=3,
            source_event=uuid4(),
        )
        UsageRecord.objects.create(
            tenant=firm.tenant,
            metric=original.metric,
            period=original.period,
            quantity=1,
            source_event=uuid4(),
            correction_of=original,
        )
        estimate = cost_estimate(firm.owner, firm.tenant.pk, on=TODAY)
        email = next(
            line for line in estimate.lines if line.metric == UsageMetric.EMAILS_SENT
        )
        assert email.quantity == 1
        assert email.measured_days == 1
        assert email.covered_through == original.period
        assert email.partial
