"""AXRM-017: every final PGMEI outcome ends in a linked guide or in owned work.

Attacks the class AXRM-013's reviews kept finding: malformed replies, a worker that
dies after the final reply, a rerun, and concurrent runners.
"""

from __future__ import annotations

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import TYPE_CHECKING, Any

import pytest
from django.db import connection, connections
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.cycles.evidence import EvidenceLink
from apps.cycles.models import Exception as CycleException
from apps.integrations.call_models import CallState, ProviderCall
from apps.integrations.models import IntegrationIssue
from apps.obligations.models import AcquisitionOrigin, Document
from apps.tenants.models import Tenant
from tests.fakes.provider import Reply
from tests.fakes.serpro import FakeSerpro
from tests.integrations.pgmei_support import (
    GUIDE_PDF,
    GuideWorld,
    LazyModule,
    das_body,
    das_reply,
    guide_world,
)

if TYPE_CHECKING:
    from apps.integrations.serpro import pgmei
else:
    pgmei = LazyModule("apps.integrations.serpro.pgmei")

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["guide_world"]


def obtain(world: GuideWorld, origin: str) -> pgmei.GuideOutcome:
    return pgmei.obtain_das_guide(
        world.owner, world.tenant_id, world.cycle.pk, origin=origin
    )


def the_call(world: GuideWorld) -> ProviderCall:
    with tenant_context(world.tenant_id):
        return ProviderCall.objects.get(operation="SP-DAS")


def guide_documents(world: GuideWorld) -> int:
    with tenant_context(world.tenant_id):
        return Document.objects.filter(
            acquisition_origin=AcquisitionOrigin.FEDERAL_PROVIDER
        ).count()


def owned_exception(world: GuideWorld, reason: str) -> CycleException | None:
    with tenant_context(world.tenant_id):
        return CycleException.objects.filter(
            cycle=world.cycle, reason_code=reason, state__in=("aberta", "em_tratamento")
        ).first()


def uncertainty_issue(world: GuideWorld) -> IntegrationIssue | None:
    with tenant_context(world.tenant_id):
        return IntegrationIssue.objects.filter(
            client=world.termo.client,
            reason="chamada_provedor_incerta",
            service="SP-DAS",
            owner__isnull=False,
        ).first()


def _wrapped(body: dict[str, Any]) -> bytes:
    return json.dumps(body).encode()


MALFORMED = {
    "not-json": Reply(body=b"<html>gateway</html>"),
    "dados-not-a-list": Reply(
        body=_wrapped({"status": 200, "mensagens": [], "dados": json.dumps({})})
    ),
    "two-guides": Reply(
        body=_wrapped(
            {
                "status": 200,
                "mensagens": [],
                "dados": json.dumps(json.loads(json.loads(das_body())["dados"]) * 2),
            }
        )
    ),
    "pdf-missing": Reply(
        body=_wrapped(
            {
                "status": 200,
                "mensagens": [],
                "dados": json.dumps([{"cnpjCompleto": "12ABC34501DE35"}]),
            }
        )
    ),
    "pdf-not-base64": Reply(
        body=das_body().replace(
            base64.b64encode(GUIDE_PDF).decode().encode(), b"@@not-base64@@"
        )
    ),
    "not-a-pdf": das_reply(pdf=b"MZ\x90\x00 executable"),
    "other-taxpayer": das_reply(cnpj="11222333000181"),
    "other-period": das_reply(period="202609"),
    "accepted-202": Reply(status=202, body=das_body(status=202)),
    "empty-204": Reply(status=204, body=b""),
}


@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_unusable_reply_is_decided_before_finality_and_owned(
    guide_world: GuideWorld, name: str
) -> None:
    # Given a reply the guide cannot be built from.
    with FakeSerpro((MALFORMED[name], das_reply())) as peer:
        # When the staff action runs, then nothing raises after the call is final.
        outcome = obtain(guide_world, peer.origin)
        assert outcome.code == "resultado_incerto"
        # A rerun the same day never repeats the issuance.
        again = obtain(guide_world, peer.origin)
        assert again.call_id == outcome.call_id
        assert len(peer.requests) == 1
    call = the_call(guide_world)
    assert call.state == CallState.UNCERTAIN
    assert guide_documents(guide_world) == 0
    with tenant_context(guide_world.tenant_id):
        assert not EvidenceLink.objects.filter(
            request__cycle=guide_world.cycle
        ).exists()
    issue = uncertainty_issue(guide_world)
    assert issue is not None
    assert str(call.pk) in issue.evidence_refs


@pytest.mark.parametrize(
    ("status", "state", "billed"),
    [
        (400, CallState.FAILED, "nao"),
        (403, CallState.DENIED, "sim"),
        (404, CallState.FAILED, "nao"),
        (429, CallState.FAILED, "nao"),
    ],
)
def test_definitive_refusal_is_final_and_opens_owned_cycle_work(
    guide_world: GuideWorld, status: int, state: str, billed: str
) -> None:
    body = _wrapped(
        {"status": status, "mensagens": [{"codigo": "x", "texto": "y"}], "dados": ""}
    )
    with FakeSerpro((Reply(status=status, body=body), das_reply())) as peer:
        outcome = obtain(guide_world, peer.origin)
        assert outcome.code == "guia_nao_obtida"
        rerun = obtain(guide_world, peer.origin)
        assert rerun.code == "guia_nao_obtida"
        assert len(peer.requests) == 1
    call = the_call(guide_world)
    assert (call.state, call.billed) == (state, billed)
    problem = owned_exception(guide_world, pgmei.GUIDE_NOT_OBTAINED)
    assert problem is not None
    assert problem.subject_ref == call.pk
    assert problem.owner_id is not None
    assert guide_documents(guide_world) == 0


def test_worker_death_after_a_final_success_is_linked_by_the_daily_sweep(
    guide_world: GuideWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a worker that dies right after the guide's call became final.
    original = pgmei.settle_guide

    def die(*args: object, **kwargs: object) -> str:
        raise SystemExit

    monkeypatch.setattr(pgmei, "settle_guide", die)
    with FakeSerpro((das_reply(), das_reply())) as peer:
        with pytest.raises(SystemExit):
            obtain(guide_world, peer.origin)
        assert len(peer.requests) == 1
    call = the_call(guide_world)
    assert call.state == CallState.SUCCESS
    # The protected reply was persisted before finality: the guide exists unlinked.
    assert guide_documents(guide_world) == 1
    with tenant_context(guide_world.tenant_id):
        assert not EvidenceLink.objects.filter(
            request__cycle=guide_world.cycle
        ).exists()
    # When the firm's daily evaluator runs, then the guide is linked, once.
    monkeypatch.setattr(pgmei, "settle_guide", original)
    tenant = Tenant.objects.get(pk=guide_world.tenant_id)
    with tenant_context(guide_world.tenant_id):
        pgmei.evaluate_guide_conditions(tenant, timezone.localdate())
        pgmei.evaluate_guide_conditions(tenant, timezone.localdate())
        links = EvidenceLink.objects.filter(request__cycle=guide_world.cycle)
        assert links.count() == 1
        assert links.get().submitted_by_id == guide_world.owner.pk


def test_worker_death_after_a_final_refusal_is_owned_by_the_daily_sweep(
    guide_world: GuideWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    def die(*args: object, **kwargs: object) -> str:
        raise SystemExit

    original = pgmei.settle_guide
    monkeypatch.setattr(pgmei, "settle_guide", die)
    with FakeSerpro((Reply(status=400),)) as peer, pytest.raises(SystemExit):
        obtain(guide_world, peer.origin)
    assert owned_exception(guide_world, pgmei.GUIDE_NOT_OBTAINED) is None
    monkeypatch.setattr(pgmei, "settle_guide", original)
    tenant = Tenant.objects.get(pk=guide_world.tenant_id)
    with tenant_context(guide_world.tenant_id):
        pgmei.evaluate_guide_conditions(tenant, timezone.localdate())
    assert owned_exception(guide_world, pgmei.GUIDE_NOT_OBTAINED) is not None


def test_rerun_after_worker_death_links_without_a_second_call(
    guide_world: GuideWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = pgmei.settle_guide
    monkeypatch.setattr(
        pgmei, "settle_guide", lambda *a, **k: (_ for _ in ()).throw(SystemExit)
    )
    with FakeSerpro((das_reply(), das_reply())) as peer:
        with pytest.raises(SystemExit):
            obtain(guide_world, peer.origin)
        monkeypatch.setattr(pgmei, "settle_guide", original)
        outcome = obtain(guide_world, peer.origin)
        assert len(peer.requests) == 1
    assert outcome.code == "guia_existente"
    assert outcome.link_id is not None


def test_linking_failure_after_finality_is_owned_and_never_raised(
    guide_world: GuideWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given the cycle closes between the click and the final reply.
    def close_cycle(*args: object, **kwargs: object) -> None:
        msg = "Este ciclo está encerrado."
        raise pgmei.IllegalTransition(msg)

    monkeypatch.setattr(pgmei, "_link_in_cycle", close_cycle)
    with FakeSerpro((das_reply(),)) as peer:
        outcome = obtain(guide_world, peer.origin)
    assert outcome.code == "guia_nao_vinculada"
    call = the_call(guide_world)
    assert call.state == CallState.SUCCESS
    problem = owned_exception(guide_world, pgmei.GUIDE_NOT_LINKED)
    assert problem is not None
    assert problem.subject_ref == call.pk


def test_concurrent_clicks_send_once(guide_world: GuideWorld) -> None:
    barrier = Barrier(2, timeout=10)

    def click(origin: str) -> pgmei.GuideOutcome | str:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET ROLE app_runtime")
            barrier.wait()
            return obtain(guide_world, origin)
        except pgmei.GuideRefused as refused:
            return refused.code
        finally:
            connections.close_all()

    with FakeSerpro((das_reply(), das_reply())) as peer:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(click, peer.origin) for _ in range(2)]
            results = [future.result(timeout=30) for future in futures]
        assert len(peer.requests) == 1
    outcomes = [row for row in results if isinstance(row, pgmei.GuideOutcome)]
    assert outcomes
    assert {row.call_id for row in outcomes} == {the_call(guide_world).pk}
    assert all(
        isinstance(row, pgmei.GuideOutcome) or row == "em_andamento" for row in results
    )


def test_uncertain_lease_loss_is_owned_and_never_resent(
    guide_world: GuideWorld,
) -> None:
    # Given a connection reset after the request left (ambiguous issuance).
    with FakeSerpro((Reply(reset=True), das_reply())) as peer:
        outcome = obtain(guide_world, peer.origin)
        assert outcome.code == "resultado_incerto"
        obtain(guide_world, peer.origin)
        assert len(peer.requests) == 1
    assert uncertainty_issue(guide_world) is not None
