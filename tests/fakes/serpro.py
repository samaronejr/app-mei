"""SERPRO scripted server with a request oracle sourced from vendor fixtures."""

import json
from pathlib import Path

from apps.integrations.serpro.qualification_wire import (
    ExpectedWire,
    inspect_wire,
    refusal_body,
)
from tests.fakes.provider import Received, Reply, ScriptedProvider

_WIRE = Path(__file__).parents[1] / "fixtures" / "providers" / "serpro" / "wire"


def _fixture_wire() -> dict[str, ExpectedWire]:
    expected = {}
    for fixture in _WIRE.glob("*.json"):
        pedido = json.loads(fixture.read_text())["body"]["pedidoDados"]
        expected[fixture.stem] = ExpectedWire(
            pedido["versaoSistema"], empty_dados=pedido["dados"] == ""
        )
    return expected


_EXPECTED = _fixture_wire()


class FakeSerpro(ScriptedProvider):
    """Script status, body, held latency, reset and oversize independently."""

    def reply_for(self, request: Received) -> Reply:
        """Refuse incorrect envelopes before consuming any scripted response."""
        # Token calls use OAuth/broker payloads, not Integra Contador envelopes.
        if request.path not in {"/authenticate", "/token"}:
            _, error = inspect_wire(request.body, _EXPECTED)
            if error:
                return Reply(status=400, body=refusal_body(error))
        return super().reply_for(request)
