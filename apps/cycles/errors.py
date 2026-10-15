"""The typed failures the playbook domain raises, each meaning exactly one thing.

The views map these onto HTTP answers, so the taxonomy is the contract: a caller that
catches `PlaybookError` catches every refusal, and one that needs to render details —
the keys forming a dependency cycle, the stale `version` that lost a race — reads them
off the subclass rather than parsing a message.

The two suffixless names follow the codebase's own precedent: `MissingTenantContext`,
`UnknownFeature`, `UnknownCapability` and `NoEffectiveDueRule` all drop the `Error`
because the name reads correctly at `raise`; the same applies here.
"""

from typing import Final


class PlaybookError(Exception):
    """Base of every refusal this domain raises."""


class PlaybookValidationError(PlaybookError):
    """The step list or `applies_to` could not be honoured, with reasons.

    `errors` is the human-readable list, rendered by the editor as a banner and
    returned by the view as a 422 body. Publishing is refused with this rather than
    trusting the form layer to have validated — a management command or a later API
    gets the same refusal the screen gives.
    """

    def __init__(self, errors: list[str]) -> None:
        """Store the list the caller renders; the message is its summary."""
        super().__init__("; ".join(errors))
        self.errors = errors


class DagCycleError(PlaybookValidationError):
    """The step dependency graph has a cycle; `cycle_keys` name it.

    A `PlaybookValidationError` subclass because the editor renders it in the
    same banner, but distinct because the 422 contract names the offending keys
    and nothing else about the failure is interested in them.
    """

    def __init__(self, cycle_keys: tuple[str, ...]) -> None:
        """Record the offending keys for the 422 body the view renders."""
        self.cycle_keys: Final = cycle_keys
        keys = ", ".join(cycle_keys)
        super().__init__([f"Dependências em ciclo entre as etapas: {keys}."])


class ImmutableVersion(PlaybookError):  # noqa: N818
    """A published or retired version was written to, through any path.

    Raised by `save()`/`delete()` on `PlaybookVersion` and `PlaybookStep` — the row
    layer is the last place a write can be refused, so the guard lives there rather
    than in the services that were supposed to call it.
    """


class VersionConflict(PlaybookError):  # noqa: N818
    """The optimistic `version` check failed: someone else wrote first.

    No write has happened. The caller reloads and retries, or reports 409 — the
    service never silently decides which of the two edits survives.
    """


class IllegalTransition(PlaybookError):  # noqa: N818
    """The state machine does not have the transition requested.

    Distinct from `PlaybookValidationError`: the steps may be perfectly
    well-formed and the version still cannot move — a published version is not
    publishable again, a draft cannot retire. Named to match the
    `IllegalTransition` the cycle state machines in T9 raise.
    """
