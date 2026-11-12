"""SERPRO scripted server; fixtures mirror the documented trial response shape."""

from tests.fakes.provider import ScriptedProvider


class FakeSerpro(ScriptedProvider):
    """Script status, body, held latency, reset and oversize independently."""
