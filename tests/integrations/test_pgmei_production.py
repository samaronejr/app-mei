"""AXRM-017: only a production guide becomes an official guide-issued fact (INV-R4)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from apps.core.tenancy import tenant_context
from apps.obligations.models import SourceObservation
from tests.fakes.serpro import FakeSerpro
from tests.integrations.pgmei_support import (
    PERIOD,
    GuideWorld,
    LazyModule,
    das_reply,
    production_world,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from apps.integrations.serpro import pgmei
else:
    pgmei = LazyModule("apps.integrations.serpro.pgmei")

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["production_world"]


def test_production_guide_writes_one_official_verified_guide_observation(
    production_world: tuple[GuideWorld, Callable[[str], None]],
) -> None:
    world, route = production_world
    with FakeSerpro((das_reply(), das_reply())) as peer:
        route(peer.origin)
        outcome = pgmei.obtain_das_guide(
            world.owner, world.tenant_id, world.cycle.pk, origin=""
        )
        assert outcome.code == "guia_obtida"
        headers = {k.lower(): v for k, v in peer.requests[0].headers}
        assert headers["autenticar_procurador_token"] == "synthetic-proc-017"  # noqa: S105
        # A rerun links nothing twice and asserts the fact once.
        pgmei.obtain_das_guide(world.owner, world.tenant_id, world.cycle.pk, origin="")
        assert len(peer.requests) == 1
    with tenant_context(world.tenant_id):
        [fact] = SourceObservation.objects.filter(kind="guide_issued")
        assert fact.subject_ref_id == world.obligation.pk
        assert fact.period == PERIOD
        assert (fact.source_kind, fact.verification_status) == (
            "official_api",
            "verified",
        )
        assert fact.evidence_link_id == outcome.link_id
        assert fact.source_ref == str(outcome.call_id)
        assert "12ABC34501DE35" not in str(fact.value_json)


def test_production_without_owner_approval_records_no_official_fact(
    production_world: tuple[GuideWorld, Callable[[str], None]],
    settings: pytest.FixtureRequest,
) -> None:
    world, route = production_world
    with FakeSerpro((das_reply(),)) as peer:
        route(peer.origin)
        settings.PROVIDER_REAL_DATA_APPROVED = False  # type: ignore[attr-defined]
        with pytest.raises(pgmei.GuideRefused):
            pgmei.obtain_das_guide(
                world.owner, world.tenant_id, world.cycle.pk, origin=""
            )
        assert peer.requests == []
    with tenant_context(world.tenant_id):
        assert not SourceObservation.objects.filter(kind="guide_issued").exists()
