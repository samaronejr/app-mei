"""SERPRO scripted server with a request oracle sourced from vendor fixtures."""

from ssl import SSLContext

from apps.integrations.serpro.qualification_wire import (
    inspect_wire,
    refusal_body,
)
from tests.fakes.provider import Received, Reply, ScriptedProvider


class FakeSerpro(ScriptedProvider):
    """Script status, body, held latency, reset and oversize independently."""

    def __init__(
        self,
        replies: tuple[Reply, ...],
        *,
        tls: SSLContext | None = None,
        trial: bool = False,
    ) -> None:
        self.trial = trial
        super().__init__(replies, tls=tls)

    def reply_for(self, request: Received) -> Reply:
        """Refuse incorrect envelopes before consuming any scripted response."""
        # Token calls use OAuth/broker payloads, not Integra Contador envelopes.
        if request.path not in {"/authenticate", "/token"}:
            _, error = inspect_wire(
                request.method, request.path, request.body, trial=self.trial
            )
            if error:
                return Reply(status=400, body=refusal_body(error))
        return super().reply_for(request)
