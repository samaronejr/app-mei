"""Budget reversal owns only its own schema and refuses populated balances."""

import pytest
from django.db import connection
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor

from apps.core.tenancy import tenant_context
from apps.integrations.budget_models import ProviderBudget
from apps.integrations.call_protocol import reserve
from apps.integrations.models import ClientProviderLink, ProviderCall
from tests.integrations.test_budget_caps import budget_link, provider_link
from tests.integrations.test_call_protocol import request
from tests.integrations.test_migration_rollback import _as_migrator

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["budget_link", "provider_link"]


def test_empty_budget_reverse_preserves_preexisting_protocol_rows(
    provider_link: ClientProviderLink,
) -> None:
    # Given a pre-budget synthetic protocol row.
    call = reserve(provider_link.tenant_id, provider_link.pk, request())
    # When only the empty budget schema is reversed.
    with _as_migrator():
        MigrationExecutor(connection).migrate([("integrations", "0003_call_protocol")])
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('integrations_providerbudget')")
            assert cursor.fetchone() == (None,)
            cursor.execute(
                "SELECT count(*) FROM pg_trigger "
                "WHERE tgname='integrations_providercallattempt_append_only'"
            )
            assert cursor.fetchone() == (1,)
    # Then the existing protocol and its shared append-only guards survive.
    with tenant_context(provider_link.tenant_id):
        assert ProviderCall.objects.get(pk=call.pk).request_key == request().request_key


def test_populated_budget_reverse_refuses_without_removing_history(
    budget_link: ClientProviderLink,
) -> None:
    # Given an explicitly provisioned budget.
    # When the schema owner attempts reversal.
    with _as_migrator(), pytest.raises(IrreversibleError, match="forward repair"):
        MigrationExecutor(connection).migrate([("integrations", "0003_call_protocol")])
    # Then its balances remain available for recovery.
    with tenant_context(budget_link.tenant_id):
        assert ProviderBudget.objects.get().cap_microbrl == 500_000
