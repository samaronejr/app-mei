"""Billed SERPRO refusal work converges for independent database sessions."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import IntegrityError, connection, connections
from psycopg.errors import UniqueViolation

from apps.audit.models import Event
from apps.core.tenancy import tenant_context
from apps.integrations import call_protocol
from apps.integrations.budget_models import ProviderBudget, ProviderReservation
from apps.integrations.call_models import CallState
from apps.integrations.models import ClientProviderLink, IntegrationIssue
from apps.integrations.serpro.models import ACTIVE_STATES, FirmIntegrationIssue
from apps.integrations.serpro.work import PROC_MISSING, own_client_result
from tests.fakes.provider import Reply
from tests.fakes.serpro import FakeSerpro
from tests.integrations.serpro_retry_support import (
    client_dispatch,
    provider_link,
    spend_link,
)
from tests.integrations.test_serpro_termo import DENIAL_016

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["provider_link", "spend_link"]


def test_billed_403_work_converges_after_both_sessions_observe_absence(
    spend_link: ClientProviderLink,
) -> None:
    # Given a real billed denial before its client/firm work has materialized.
    with FakeSerpro((Reply(status=403, body=DENIAL_016), Reply())) as peer:
        row, ticket, adapter = client_dispatch(spend_link, peer.origin)
        result = call_protocol._perform_once(ticket, adapter)
        assert (result.state, result.billed) == (CallState.DENIED, "sim")
        assert len(peer.requests) == 1
    absent = Barrier(2, timeout=10)

    def contend() -> tuple[int, str]:
        stopped = False

        def after_query(
            execute: Callable[..., object],
            sql: str,
            params: object,
            many: bool,
            context: object,
        ) -> object:
            nonlocal stopped
            value = execute(sql, params, many, context)
            if (
                not stopped
                and sql.startswith("SELECT")
                and 'FROM "integrations_integrationissue"' in sql
            ):
                stopped = True
                absent.wait()
            return value

        try:
            with connection.cursor() as cursor:
                cursor.execute("SET ROLE app_runtime")
                cursor.execute("SET statement_timeout = '10s'")
                cursor.execute("SELECT pg_backend_pid()")
                backend = cursor.fetchone()
                assert backend is not None
                backend_id = int(backend[0])
            try:
                with connection.execute_wrapper(after_query):
                    own_client_result(ticket)
            except IntegrityError as error:
                cause = error.__cause__
                if not isinstance(cause, UniqueViolation):
                    raise
                return backend_id, f"IntegrityError:{cause.diag.constraint_name}"
            return backend_id, "converged"
        finally:
            connections.close_all()

    # When both SELECTs return before either session can insert the active issue.
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(contend), pool.submit(contend)]
        outcomes = [future.result(timeout=15) for future in futures]
    # Then both callers finish and the one owned row in each scope stays minimized.
    assert len({backend for backend, _ in outcomes}) == 2
    assert sorted(outcome for _, outcome in outcomes) == ["converged", "converged"]
    with tenant_context(spend_link.tenant_id):
        client_issue = IntegrationIssue.objects.get(
            client=spend_link.client, reason=PROC_MISSING, state="aberta"
        )
        firm_issue = FirmIntegrationIssue.objects.get(
            connection=spend_link.connection,
            reason=PROC_MISSING,
            state__in=ACTIVE_STATES,
        )
        assert client_issue.owner_id is not None
        assert firm_issue.owner_id is not None
        assert client_issue.evidence_refs == firm_issue.evidence_refs == [str(row.pk)]
        assert (
            Event.objects.filter(
                action="integrations.issue_opened", object_id=str(client_issue.pk)
            ).count()
            == 1
        )
        assert (
            Event.objects.filter(
                action="integrations.firm_issue_opened", object_id=str(firm_issue.pk)
            ).count()
            == 1
        )
        assert ProviderReservation.objects.get(call=row).state == "known"
        assert ProviderBudget.objects.get().known_liability == 240_000
