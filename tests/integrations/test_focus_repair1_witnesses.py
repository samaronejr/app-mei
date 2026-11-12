"""AXRM-019 repair 1 witnesses: the send-time fence lock, write and command."""

import json

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from pytest_django.fixtures import SettingsWrapper

from apps.audit.models import Event
from apps.core.tenancy import tenant_context
from apps.integrations.allowlist import resolve
from apps.integrations.focus import company as focus_company
from apps.integrations.focus.company import FocusRefusedError, plan_setup
from apps.integrations.management.commands import focus_company_setup
from apps.integrations.transport_types import ProviderRequest
from tests.integrations.test_focus_adapter import World, world
from tests.integrations.test_focus_adapter_edges import NFSEN, _setup, outcome_of

__all__ = ["world"]
db = pytest.mark.django_db(transaction=True)


@db
def test_the_fence_reads_the_link_under_a_row_lock(world: World) -> None:
    plan = plan_setup(world.client.pk, "nfsen_recebidas", "2026-11")
    with CaptureQueriesContext(connection) as queries:
        focus_company._fence_start_month(plan, dry_run=True)
    reads = [
        q["sql"]
        for q in queries.captured_queries
        if "integrations_clientproviderlink" in q["sql"]
        and q["sql"].lstrip().upper().startswith("SELECT")
    ]
    assert any("FOR UPDATE" in sql for sql in reads), reads


@db
def test_the_month_is_written_once_with_one_version_bump(world: World) -> None:
    before = world.reload().version
    assert outcome_of(_setup(world, *NFSEN)) == "ok"
    assert outcome_of(_setup(world, *NFSEN)) == "ok"
    with tenant_context(world.tenant.pk):
        versions = [
            meta["version"]
            for meta in Event.objects.filter(
                action="integrations.capture_start_set"
            ).values_list("metadata", flat=True)
        ]
    assert versions == [before + 1]


@db
def test_a_send_time_refusal_is_the_exact_command_error(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(*_args: object, **_kwargs: object) -> None:
        message = "mes divergente"
        raise FocusRefusedError(message)

    monkeypatch.setattr(focus_company_setup, "run_setup", refused)
    assert outcome_of(_setup(world, *NFSEN)) == "CommandError: mes divergente"


def test_an_absent_qualification_setting_keeps_received_nfe_closed(
    settings: SettingsWrapper,
) -> None:
    # N09: the fallback, not just the configured blank, must be closed.
    del settings.FOCUS_NFE_RECEBIDAS_QUALIFICATION_REF
    body = json.dumps({"habilita_manifestacao_homologacao": True}).encode()
    request = ProviderRequest(variant="update", subject="4321", body=body)
    assert outcome_of(lambda: resolve("focus", "FC-EMP", request)) == (
        "OperacaoNaoPermitidaError: operacao_nao_permitida"
    )
