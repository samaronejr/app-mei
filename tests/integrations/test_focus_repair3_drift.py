"""AXRM-019 repair 3: status sync surfaces a receipt switch reopened after a disable.

A crash between a setup's write and its compensation can leave a receipt switch on at
Focus while the link is confirmed disabled. The capture sweep already skips the link;
status sync must make the mismatch visible: an owned issue and an audit event naming
the switches, so the operator re-runs focus_company_disable. Nothing is sent from the
sync, and active links are unaffected.
"""

import pytest

from apps.audit.models import Event
from apps.core.tenancy import tenant_context
from apps.integrations.call_models import CallState
from apps.integrations.focus.company import sync_company_status
from apps.integrations.models import IntegrationIssue
from tests.integrations.test_focus_adapter import World, activate, world

__all__ = ["world"]
db = pytest.mark.django_db(transaction=True)
FLAG = "habilita_nfsen_recebidas_homologacao"
NFE_FLAG = "habilita_manifestacao_homologacao"
REASON = "recebimento_reaberto_provedor"
EVENT = "integrations.focus_receipt_reopened"


def _flagged(world: World) -> tuple[list[str], list[dict[str, object]]]:
    with tenant_context(world.tenant.pk):
        issues = [
            str(link_id)
            for link_id in IntegrationIssue.objects.filter(reason=REASON).values_list(
                "link_id", flat=True
            )
        ]
        events = list(
            Event.objects.filter(action=EVENT).values_list("metadata", flat=True)
        )
    return issues, events


@db
def test_a_switch_on_at_focus_on_a_confirmed_disabled_link_is_surfaced(
    world: World,
) -> None:
    # Given a confirmed disable whose NFS-e switch is back on at Focus.
    activate(world, disablement_state="desativada_no_provedor_confirmada")
    world.fake.company[FLAG] = True
    # When status sync reads the company.
    assert sync_company_status(world.link).state == CallState.SUCCESS
    # Then one issue names the link and one event names the switch; nothing was sent.
    issues, events = _flagged(world)
    assert issues == [str(world.link.pk)]
    assert [(e["switches"], e["needs_redisable"]) for e in events] == [([FLAG], True)]
    assert [r.method for r in world.fake.requests] == ["GET"]


@db
@pytest.mark.parametrize(
    ("state", "on"),
    [("desativada_no_provedor_confirmada", False), ("ativa", True)],
)
def test_no_issue_when_the_switch_is_off_or_the_link_is_active(
    world: World, state: str, on: bool
) -> None:
    activate(world, disablement_state=state)
    world.fake.company[FLAG] = on
    assert sync_company_status(world.link).state == CallState.SUCCESS
    assert _flagged(world) == ([], [])


@db
def test_a_switch_on_for_a_stream_not_configured_locally_is_surfaced(
    world: World,
) -> None:
    # The link records only NFS-e; Focus reports NF-e receipt on.
    activate(world, disablement_state="desativada_no_provedor_confirmada")
    world.fake.company[FLAG] = False
    world.fake.company[NFE_FLAG] = True
    assert sync_company_status(world.link).state == CallState.SUCCESS
    issues, events = _flagged(world)
    assert issues == [str(world.link.pk)]
    assert [e["switches"] for e in events] == [[NFE_FLAG]]
