"""Focus scripted server, including one-hop PDF redirects and receipt streams."""

from tests.fakes.provider import ScriptedProvider


class FakeFocus(ScriptedProvider):
    """Use the same wire script semantics as SERPRO, on an independent port."""
