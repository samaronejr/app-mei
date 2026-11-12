"""AXRM-019 repair 3: a disable decides what to turn off inside the link lock.

RV3-WHOLE-LINK-DISABLE-MISSES-CONCURRENTLY-ADDED-STREAM: a disable planned before a
setup added a stream must still turn that stream off (plan: after focus_company_disable
the fake read shows the flags false and the sweep skips the client). A whole-link
disable sends every receipt switch the adapter manages as false; a stream-scoped
disable touches only its stream, both decided from the link as stored under the lock.
"""

import pytest
from pytest_django.fixtures import SettingsWrapper

from apps.integrations.focus.company import plan_disable, run_disable
from tests.integrations.test_focus_adapter import (
    World,
    _capture,
    activate,
    setup,
    world,
)

__all__ = ["world"]
db = pytest.mark.django_db(transaction=True)
NFSEN_FLAG = "habilita_nfsen_recebidas_homologacao"
NFE_FLAG = "habilita_manifestacao_homologacao"
QUALIFICATION = "AXRM-PQ-FOCUS-1-2026-11-05"


def _add_nfe(world: World, settings: SettingsWrapper) -> None:
    settings.FOCUS_NFE_RECEBIDAS_QUALIFICATION_REF = QUALIFICATION
    setup(world, "--streams", "nfe_recebidas", "--start-month", "2026-11")
    assert world.fake.company[NFE_FLAG] is True


@db
def test_a_whole_link_disable_planned_before_a_setup_stops_the_added_stream(
    world: World, settings: SettingsWrapper
) -> None:
    # Given a whole-link disable planned while only NFS-e was configured.
    activate(world)
    stale = plan_disable(world.client.pk)
    # When a setup adds NF-e before the disable runs.
    _add_nfe(world, settings)
    outcome = run_disable(stale)
    # Then both switches are off at Focus, confirmed, and capture skips the client.
    assert outcome.confirmed
    assert world.fake.company[NFE_FLAG] is False
    assert world.fake.company[NFSEN_FLAG] is False
    assert world.reload().disablement_state == "desativada_no_provedor_confirmada"
    assert _capture(world) == []


@db
def test_a_whole_link_disable_turns_off_a_switch_not_configured_locally(
    world: World,
) -> None:
    # Given NF-e receipt on at Focus although the link records only NFS-e.
    activate(world)
    world.fake.company[NFE_FLAG] = True
    # When the operator disables the whole link.
    outcome = run_disable(plan_disable(world.client.pk))
    # Then every receipt switch the adapter manages is off, confirmed by read-back.
    assert outcome.confirmed
    assert world.fake.company[NFE_FLAG] is False
    assert world.fake.company[NFSEN_FLAG] is False


@db
def test_a_stream_disable_planned_before_a_setup_keeps_the_added_stream(
    world: World, settings: SettingsWrapper
) -> None:
    # Given an NFS-e-only disable planned while NFS-e was the only stream.
    activate(world)
    stale = plan_disable(world.client.pk, ["nfsen_recebidas"])
    # When a setup adds NF-e before the disable runs.
    _add_nfe(world, settings)
    assert run_disable(stale).confirmed
    # Then only NFS-e stops; NF-e stays configured and the link stays active.
    link = world.reload()
    assert link.disablement_state == "ativa"
    assert list(link.streams) == ["nfe_recebidas"]
    assert world.fake.company[NFSEN_FLAG] is False
    assert world.fake.company[NFE_FLAG] is True
