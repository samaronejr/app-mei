"""A provider admission must serialize with the existing audited link writer."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from django.db import OperationalError, connection, connections
from psycopg.errors import DeadlockDetected

from apps.audit.models import Event as AuditEvent
from apps.core.tenancy import tenant_context
from apps.integrations.call_contracts import AdmissionDeniedError
from apps.integrations.call_models import ProviderCall
from apps.integrations.call_protocol import reserve
from apps.integrations.models import ClientProviderLink, LinkStatus
from apps.integrations.services import LinkMachine, transition_link
from tests.cycles.approval_fixtures import SIGNAL_TIMEOUT, LockSignal
from tests.integrations.test_call_protocol import provider_link, request

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["provider_link"]


def runtime(fn: Callable[[], str]) -> str:
    """Keep phased services outside a transaction and report either deadlock victim."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE app_runtime")
            cursor.execute("SET statement_timeout = '10s'")
        return fn()
    except OperationalError as error:
        if isinstance(error.__cause__, DeadlockDetected):
            return "deadlock"
        raise
    finally:
        connections.close_all()


def test_audited_link_revocation_commits_before_waiting_admission(
    provider_link: ClientProviderLink,
) -> None:
    # Given the real writer holding its link, before its audit insert/commit.
    link_held, release_writer, tenant_held = Event(), Event(), Event()

    def revoke() -> str:
        with connection.execute_wrapper(
            LockSignal(
                "integrations_clientproviderlink",
                link_held,
                release_writer,
                after=True,
            )
        ):
            transition_link(
                provider_link.tenant_id,
                provider_link.pk,
                machine=LinkMachine.STATUS,
                to=LinkStatus.REVOGADA,
                expected_version=1,
            )
        return "updated"

    def admit() -> str:
        with connection.execute_wrapper(
            LockSignal(
                "tenants_tenant",
                tenant_held,
                after=True,
            )
        ):
            try:
                reserve(provider_link.tenant_id, provider_link.pk, request())
            except AdmissionDeniedError:
                return "denied"
        return "reserved"

    # When admission holds its tenant lock and waits for the same link writer.
    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(runtime, revoke)
        try:
            assert link_held.wait(SIGNAL_TIMEOUT)
            admitting = pool.submit(runtime, admit)
            assert tenant_held.wait(SIGNAL_TIMEOUT)
        finally:
            release_writer.set()
        outcomes = (
            writing.result(timeout=SIGNAL_TIMEOUT),
            admitting.result(timeout=SIGNAL_TIMEOUT),
        )

    # Then the audit FK can commit, admission sees revocation, and no call is reserved.
    assert outcomes == ("updated", "denied")
    with tenant_context(provider_link.tenant_id):
        assert not ProviderCall.objects.exists()
        assert AuditEvent.objects.filter(
            action="integrations.link_state_changed",
            object_id=str(provider_link.pk),
        ).exists()
