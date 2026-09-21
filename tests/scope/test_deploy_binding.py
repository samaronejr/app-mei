"""The deploy job's public assertions must never trust DNS.

Every public assertion in the push deploy pins resolution with
`curl --resolve samaronefialho.dev:443:$LIGHTSAIL_IP`, where the address comes from
the Lightsail control plane — not from a resolver. That is what makes them claims
about the host just deployed rather than about whatever DNS currently points at, and
it is load-bearing precisely while the two disagree: between the cutover merge and
the DNS flip, `samaronefialho.dev` still resolves to the old box, so an assertion
that resolved the name would pass against the wrong machine.

`dig +short` is the shape that regression takes: a deploy step that asks DNS for the
target's address and then asserts against the answer. This guard forbids the token
outright in `ci.yml`, so the mistake fails in CI rather than on the first deploy
after somebody "simplifies" a `--resolve` away.
"""

from pathlib import Path
from typing import Final

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
CI_WORKFLOW: Final[Path] = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"

# The forbidden token. `dig +short` is how a step would ask DNS for the target's
# address; the deploy must only ever learn it from the Lightsail control plane.
# S105 pattern-matches on the variable name; the value is a shell token, not a
# password, so the inline noqa is the honest annotation.
DNS_LOOKUP_TOKEN: Final = "dig +short"  # noqa: S105


def test_the_push_deploy_never_resolves_its_target_through_dns() -> None:
    # Given the workflow that owns the push deploy
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    # When it is read for a DNS lookup of the deploy target
    # Then there is none. A `dig +short` anywhere in this file is a deploy assertion
    # that trusts whatever DNS says today — which, during cutover, is the old box.
    assert DNS_LOOKUP_TOKEN not in workflow

    # And the guard is not vacuous: the pinned-resolution assertions it protects are
    # still there to be protected.
    assert "--resolve" in workflow


def test_the_guard_fires_on_a_dns_lookup() -> None:
    """The positive control: the same check must fail on a mutated file."""
    # Given the real workflow with a DNS lookup planted in it
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    mutated = workflow + "\n          dig +short samaronefialho.dev\n"

    # When the guard's own predicate is applied to the mutation
    # Then it fires. Without this control the test above would pass just as happily
    # against a guard that could never fail.
    assert DNS_LOOKUP_TOKEN in mutated
