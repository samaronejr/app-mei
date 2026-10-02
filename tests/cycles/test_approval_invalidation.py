"""Every S7.4 cell plus comparison of the bytes that an approval actually binds."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import assert_never

import pytest

from apps.cycles.effects.invalidation import (
    Change,
    Impact,
    RevalidationContext,
    assess_changes,
    compare_manifests,
)
from apps.cycles.effects.manifest import ActionKind, EffectManifestV1
from tests.cycles.manifest_fixtures import FIRST, NOW, THIRD, manifest_example

KINDS = [
    (ActionKind.CLIENT_PUBLICATION, 0),
    (ActionKind.EMAIL_NOTICE, 1),
    (ActionKind.CYCLE_COMPLETION, 2),
    (ActionKind.REQUEST_WAIVER, 3),
]
MATRIX_CASES = [
    (Change.RECIPIENTS, ("invalidate", "invalidate", "ignore", "ignore")),
    (Change.DOCUMENTS, ("invalidate", "invalidate", "invalidate", "ignore")),
    (Change.EVIDENCE, ("invalidate", "ignore", "invalidate", "invalidate")),
    (Change.AMOUNT, ("invalidate", "invalidate", "invalidate", "ignore")),
    (Change.POLICY, ("invalidate", "invalidate", "invalidate", "invalidate")),
    (Change.APPROVER, ("invalidate", "invalidate", "invalidate", "invalidate")),
    (Change.ACCESS_EXPIRED, ("invalidate", "invalidate", "invalidate", "invalidate")),
    (Change.SOURCE_STALE, ("invalidate", "warn", "invalidate", "ignore")),
    (Change.EXPIRY, ("invalidate", "invalidate", "invalidate", "invalidate")),
    (Change.BODY, ("invalidate", "invalidate", "ignore", "ignore")),
]


@pytest.fixture
def manifest() -> EffectManifestV1:
    return manifest_example()


@pytest.mark.parametrize(("kind", "column"), KINDS)
@pytest.mark.parametrize(("change", "row"), MATRIX_CASES)
def test_every_matrix_policy_cell(
    kind: ActionKind, column: int, change: Change, row: tuple[str, ...]
) -> None:
    reasons = assess_changes(kind, (change,))

    expected = [] if row[column] == "ignore" else [row[column]]
    assert [reason.impact.value for reason in reasons] == expected
    assert all(reason.code == change.value for reason in reasons)


def _change(
    manifest: EffectManifestV1, change: Change
) -> tuple[EffectManifestV1, EffectManifestV1, RevalidationContext]:
    before = replace(
        manifest,
        authorization={
            **manifest.authorization,
            "grants": tuple(
                {**grant, "expires_at": None}
                for grant in manifest.authorization["grants"]
            ),
        },
    )
    after = before
    context = RevalidationContext(
        now=NOW, approver_authorized=True, grant_access_valid=True
    )
    match change:
        case Change.RECIPIENTS:
            after = replace(
                before,
                recipients=(
                    {
                        "role": "to",
                        "contact_id": THIRD,
                        "channel": "email",
                        "address": "new-recipient@example.com",
                    },
                ),
            )
        case Change.DOCUMENTS:
            after = replace(
                before,
                documents=(
                    {**before.documents[0], "sha256": "ef" * 32, "link_revision": 3},
                ),
            )
        case Change.EVIDENCE:
            after = replace(before, evidence=({**before.evidence[0], "revision": 5},))
        case Change.AMOUNT:
            after = replace(
                before,
                money=(replace(before.money[0], amount=Decimal("500.00")),),
            )
        case Change.POLICY:
            after = replace(before, policy={**before.policy, "version": 5})
        case Change.APPROVER | Change.ACCESS_EXPIRED:
            context = replace(
                context,
                approver_authorized=change is not Change.APPROVER,
                grant_access_valid=change is not Change.ACCESS_EXPIRED,
            )
        case Change.SOURCE_STALE:
            before = replace(before, expires_at=NOW + timedelta(days=30))
            after = before
            context = replace(context, now=NOW + timedelta(days=8))
        case Change.EXPIRY:
            context = replace(context, now=NOW + timedelta(days=4))
        case Change.BODY:
            after = replace(
                before, payload={**before.payload, "body": "A different rendered body"}
            )
        case unknown:
            assert_never(unknown)
    return before, after, context


@pytest.mark.parametrize(("kind", "column"), KINDS)
@pytest.mark.parametrize(("change", "row"), MATRIX_CASES)
def test_frozen_byte_comparison_obeys_each_matrix_cell(
    manifest: EffectManifestV1,
    kind: ActionKind,
    column: int,
    change: Change,
    row: tuple[str, ...],
) -> None:
    before, after, context = _change(replace(manifest, action_kind=kind), change)
    before_bytes, after_bytes = before.canonical_bytes(), after.canonical_bytes()

    reasons = compare_manifests(before_bytes, after_bytes, context=context)

    expected = [] if row[column] == "ignore" else [(change.value, row[column])]
    assert [(reason.code, reason.impact.value) for reason in reasons] == expected
    if change is Change.RECIPIENTS and row[column] == "invalidate":
        # Required negative control: dropping recipients from the manifest must
        # make this fail, even if a comparator inspects some separate live object.
        assert before_bytes != after_bytes


def test_reordered_recipient_set_does_not_invalidate(
    manifest: EffectManifestV1,
) -> None:
    context = RevalidationContext(
        now=NOW, approver_authorized=True, grant_access_valid=True
    )
    reordered = replace(manifest, recipients=tuple(reversed(manifest.recipients)))

    assert (
        compare_manifests(
            manifest.canonical_bytes(), reordered.canonical_bytes(), context=context
        )
        == ()
    )


def test_removing_a_linked_document_invalidates_an_email(
    manifest: EffectManifestV1,
) -> None:
    email = replace(manifest, action_kind=ActionKind.EMAIL_NOTICE)
    context = RevalidationContext(
        now=NOW, approver_authorized=True, grant_access_valid=True
    )

    reasons = compare_manifests(
        email.canonical_bytes(),
        replace(email, documents=()).canonical_bytes(),
        context=context,
    )

    assert [reason.code for reason in reasons] == [Change.DOCUMENTS.value]
    assert reasons[0].invalidates


def test_expired_frozen_grant_cannot_be_overridden_by_a_live_allow_result(
    manifest: EffectManifestV1,
) -> None:
    context = RevalidationContext(
        now=NOW + timedelta(days=2),
        approver_authorized=True,
        grant_access_valid=True,
    )

    reasons = compare_manifests(
        manifest.canonical_bytes(), manifest.canonical_bytes(), context=context
    )

    assert [reason.code for reason in reasons] == [Change.ACCESS_EXPIRED.value]
    assert all(reason.invalidates for reason in reasons)


def test_future_not_before_blocks_execution_without_invalidating_approval(
    manifest: EffectManifestV1,
) -> None:
    future = replace(manifest, not_before=NOW + timedelta(hours=1))
    context = RevalidationContext(
        now=NOW, approver_authorized=True, grant_access_valid=True
    )

    reasons = compare_manifests(
        future.canonical_bytes(), future.canonical_bytes(), context=context
    )

    assert [(reason.code, reason.impact) for reason in reasons] == [
        ("not_before", Impact.WAIT)
    ]
    assert reasons[0].blocks_execution
    assert not reasons[0].invalidates


def test_unlisted_identity_changes_fail_closed(manifest: EffectManifestV1) -> None:
    context = RevalidationContext(
        now=NOW, approver_authorized=True, grant_access_valid=True
    )

    reasons = compare_manifests(
        manifest.canonical_bytes(),
        replace(manifest, proposal_id=FIRST).canonical_bytes(),
        context=context,
    )

    assert [reason.code for reason in reasons] == ["manifest_identity_changed"]
    assert reasons[0].invalidates
