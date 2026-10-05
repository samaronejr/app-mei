"""How an observation row becomes a renderable state (T17).

One adapter, three surfaces: the portal's per-obligation label, the staff
coverage panel, and the render-policy test all ask the same question — "what do
these source columns mean for the person looking at it?" — so the mapping is a
function rather than a ladder of template `if`s that can drift apart. The
portal template keeps owning its pt-BR wording; this module answers only the
state name.

The rule the mutation test exists to keep honest: a `staff_recorded` fact maps
to `manual`, never to `verified`. Verification is a source property — an
official source produced it — and a person writing a row cannot become one.
"""

from typing import Final

MANUAL_SOURCES: Final = frozenset(
    {"staff_recorded", "client_submitted", "legacy_status"}
)
OFFICIAL_SOURCES: Final = frozenset({"official_api", "official_file_import"})

# States the adapter may return. `manual` covers every people-sourced claim,
# including a submitted receipt still awaiting review — informed but
# unverified, which is exactly what "manual" says.
PAYMENT_STATES: Final = frozenset({"verified", "manual", "stale", "unknown"})


def payment_state(source_kind: str, verification_status: str, outcome: str) -> str:
    """Classify one payment observation for rendering.

    A rejection or retraction is asserted knowledge about a claim — but the
    knowledge it asserts is "this proves nothing", which renders as unknown,
    not as a special visual.
    """
    if outcome in {"rejected", "retracted"} or verification_status == "unsupported":
        return "unknown"
    if source_kind in MANUAL_SOURCES:
        return "manual"
    if source_kind in OFFICIAL_SOURCES:
        if verification_status == "verified":
            return "verified"
        if verification_status in {"stale", "refresh_failed"}:
            return "stale"
    return "unknown"
