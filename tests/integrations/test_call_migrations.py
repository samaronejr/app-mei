"""Protocol migrations preserve older populated tables and shared audit guards."""

from datetime import date
from uuid import uuid4

import pytest
from django.db import connection
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor

from apps.core.tenancy import tenant_context
from apps.integrations.call_models import ProviderCall
from apps.integrations.call_protocol import reserve
from apps.integrations.models import ClientProviderLink, ProviderConnection
from apps.tenants.models import Tenant, UsageMetric, UsageRecord
from tests.integrations.test_call_protocol import provider_link, request
from tests.integrations.test_migration_rollback import _as_migrator

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["provider_link"]


def test_empty_protocol_reverse_preserves_populated_preexisting_tables() -> None:
    # Given a pre-existing connection and immutable usage receipt.
    tenant = Tenant.objects.create(name="Migration survivor", slug="migration-survivor")
    with tenant_context(tenant.pk):
        provider = ProviderConnection.objects.create(
            tenant=tenant, provider="serpro", environment="fake"
        )
        receipt = UsageRecord.objects.create(
            tenant=tenant,
            metric=UsageMetric.EMAILS_SENT,
            period=date(2026, 10, 23),
            quantity=1,
            source_event=uuid4(),
        )
    # When the empty protocol and its metric extension are reversed as their owner.
    with _as_migrator():
        MigrationExecutor(connection).migrate(
            [("tenants", "0014_invite_originating_contact")]
        )
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('integrations_providercall')")
            assert cursor.fetchone() == (None,)
            cursor.execute(
                "SELECT count(*) FROM pg_trigger "
                "WHERE tgname='tenants_usagerecord_append_only'"
            )
            assert cursor.fetchone() == (1,)
    # Then only the created schema was reversed; unrelated rows and guards survive.
    with tenant_context(tenant.pk):
        assert ProviderConnection.objects.get(pk=provider.pk).provider == "serpro"
        assert UsageRecord.objects.get(pk=receipt.pk).quantity == 1


def test_populated_protocol_refuses_reverse_without_losing_receipts(
    provider_link: ClientProviderLink,
) -> None:
    # Given a reserved row that must survive operational rollback.
    row = reserve(provider_link.tenant_id, provider_link.pk, request())
    # When the schema owner attempts to reverse its protocol migration.
    with _as_migrator(), pytest.raises(IrreversibleError, match="forward repair"):
        MigrationExecutor(connection).migrate(
            [("integrations", "0002_authority_records")]
        )
    # Then the refusal preserves the call rather than deleting operational state.
    with tenant_context(provider_link.tenant_id):
        assert ProviderCall.objects.get(pk=row.pk).request_key == request().request_key


def test_provider_usage_metric_reverse_refuses_populated_provider_usage() -> None:
    # Given an immutable provider receipt, independent of an outbox row.
    tenant = Tenant.objects.create(name="Usage survivor", slug="usage-survivor")
    with tenant_context(tenant.pk):
        receipt = UsageRecord.objects.create(
            tenant=tenant,
            metric=UsageMetric.SERPRO_EMISSAO,
            period=date(2026, 10, 23),
            quantity=1,
            source_event=uuid4(),
        )
    # When reversing the added metric CHECK would invalidate that receipt.
    with _as_migrator(), pytest.raises(IrreversibleError, match="forward repair"):
        MigrationExecutor(connection).migrate(
            [("tenants", "0014_invite_originating_contact")]
        )
    # Then the additive extension refuses and keeps the receipt.
    with tenant_context(tenant.pk):
        assert (
            UsageRecord.objects.get(pk=receipt.pk).metric == UsageMetric.SERPRO_EMISSAO
        )
