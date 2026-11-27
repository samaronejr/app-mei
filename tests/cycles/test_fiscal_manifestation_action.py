"""Exact finite Ciencia batches extend V1 without changing the R1 vectors."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import cast
from uuid import uuid4

import pytest
from django.core.exceptions import ValidationError

from apps.cycles.effects.inputs import ProposalInput
from apps.cycles.effects.invalidation import (
    Change,
    Impact,
    RevalidationContext,
    assess_changes,
    compare_manifests,
)
from apps.cycles.effects.manifest import (
    BOUND_COLLECTIONS,
    MESSAGE_ACTIONS,
    ActionKind,
    EffectManifestV1,
    FiscalDocumentEffect,
)
from tests.cycles.manifest_fixtures import (
    FIRST,
    HASH,
    NOW,
    SECOND,
    THIRD,
    manifest_example,
)
from tests.integrations.capture_fixtures import key
from tests.integrations.ciencia_fixtures import (
    manifestation_kind,
)


def _manifest() -> EffectManifestV1:
    kind = manifestation_kind()
    sample = manifest_example()
    return replace(
        sample,
        action_kind=kind,
        adapter_version="focus.ciencia.v1",
        confirmation_contract_version="focus.ciencia.readback.v1",
        destination={
            "provider": "focus",
            "account_id": FIRST.upper(),
            "operation": "FC-CIE",
        },
        payload={
            "event_type": "ciencia",
            "environment": "fake",
            "maximum_cost_microbrl": 0,
        },
        documents=(
            FiscalDocumentEffect(
                chave=key(2),
                fiscal_revision_id=SECOND.upper(),
                semantic_digest="EF" * 32,
                environment="fake",
            ),
            FiscalDocumentEffect(
                chave=key(1),
                fiscal_revision_id=FIRST.upper(),
                semantic_digest=HASH.upper(),
                environment="fake",
            ),
        ),
        authorization={
            **sample.authorization,
            "client_authority": {
                "id": FIRST.upper(),
                "version": 2,
                "evidence_document_id": THIRD.upper(),
            },
        },
    )


def test_new_kind_has_a_fixed_golden_digest_and_only_its_effective_bindings() -> None:
    manifest = _manifest()
    data = json.loads(manifest.canonical_bytes())

    assert manifest.digest == (
        "cabdadf3798d5e2ee4c7479c4d34a4e70b611bb0406bddc0dd87a7002537d12e"
    )
    # Set members sort by their complete canonical bytes. The dated expiry sorts
    # before null, even though its grant id sorts after the perpetual grant's id.
    assert [row["id"] for row in data["authorization"]["grants"]] == [THIRD, FIRST]
    assert data["schema"] == "axorim.effect.v1"
    assert BOUND_COLLECTIONS[manifest.action_kind] == frozenset(
        {"documents", "authorization"}
    )
    assert manifest.action_kind not in MESSAGE_ACTIONS
    assert data["documents"] == [
        {
            "chave": key(1),
            "fiscal_revision_id": FIRST,
            "semantic_digest": HASH,
            "environment": "fake",
        },
        {
            "chave": key(2),
            "fiscal_revision_id": SECOND,
            "semantic_digest": "ef" * 32,
            "environment": "fake",
        },
    ]
    assert data["authorization"]["client_authority"] == {
        "id": FIRST,
        "version": 2,
        "evidence_document_id": THIRD,
    }
    assert all(
        data[name] == [] for name in ("recipients", "money", "evidence", "observations")
    )
    assert (
        replace(
            manifest, documents=tuple(reversed(manifest.documents))
        ).canonical_bytes()
        == manifest.canonical_bytes()
    )


@pytest.mark.parametrize(
    ("change", "impact"),
    [
        (Change.RECIPIENTS, None),
        (Change.DOCUMENTS, Impact.INVALIDATE),
        (Change.EVIDENCE, None),
        (Change.AMOUNT, None),
        (Change.POLICY, Impact.INVALIDATE),
        (Change.APPROVER, Impact.INVALIDATE),
        (Change.ACCESS_EXPIRED, Impact.INVALIDATE),
        (Change.SOURCE_STALE, None),
        (Change.EXPIRY, Impact.INVALIDATE),
        (Change.BODY, None),
    ],
)
def test_new_kind_defines_every_invalidation_matrix_cell(
    change: Change, impact: Impact | None
) -> None:
    reasons = assess_changes(manifestation_kind(), (change,))
    assert [reason.impact for reason in reasons] == ([] if impact is None else [impact])


@pytest.mark.parametrize("changed", ["revision", "semantic_digest", "environment"])
def test_every_bound_fiscal_fact_changes_the_frozen_digest(changed: str) -> None:
    before = _manifest()
    row = cast("FiscalDocumentEffect", before.documents[0]).copy()
    if changed == "revision":
        row["fiscal_revision_id"] = THIRD
    elif changed == "semantic_digest":
        row["semantic_digest"] = "12" * 32
    else:
        row["environment"] = "homologacao"
    after = replace(before, documents=(row, before.documents[1]))

    assert before.digest != after.digest
    reasons = compare_manifests(
        before.canonical_bytes(),
        after.canonical_bytes(),
        context=RevalidationContext(
            now=NOW, approver_authorized=True, grant_access_valid=True
        ),
    )
    assert "documents_changed" in {reason.code for reason in reasons}
    assert any(reason.invalidates for reason in reasons)


def test_authority_revision_participates_in_the_v1_authorization_binding() -> None:
    before = _manifest()
    after = replace(
        before,
        authorization={
            **before.authorization,
            "client_authority": {
                "id": SECOND,
                "version": 3,
                "evidence_document_id": THIRD,
            },
        },
    )
    reasons = compare_manifests(
        before.canonical_bytes(),
        after.canonical_bytes(),
        context=RevalidationContext(
            now=NOW, approver_authorized=True, grant_access_valid=True
        ),
    )
    assert before.digest != after.digest
    assert "authorization_changed" in {reason.code for reason in reasons}


@pytest.mark.parametrize("count", [0, 101])
def test_batch_size_is_finite_and_bounded_at_the_input_boundary(count: int) -> None:
    kind = manifestation_kind()
    inputs = ProposalInput(
        action_kind=kind,
        step_key="ciencia",
        provider_link_id=uuid4(),
        fiscal_document_ids=tuple(uuid4() for _ in range(count)),
    )
    with pytest.raises(ValidationError):
        inputs.validate()


def test_a_fiscal_selection_cannot_be_hidden_inside_another_action_kind() -> None:
    manifestation_kind()
    inputs = ProposalInput(
        action_kind=ActionKind.REQUEST_WAIVER,
        step_key="waive",
        evidence_request_ids=(uuid4(),),
        reason="Synthetic reason",
        provider_link_id=uuid4(),
        fiscal_document_ids=(uuid4(),),
    )
    with pytest.raises(ValidationError):
        inputs.validate()
