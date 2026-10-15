"""The closed S7.4 value: normalized sets, frozen inputs and no render-time IDs."""

import json
from dataclasses import replace
from decimal import ROUND_HALF_EVEN

import pytest

from apps.cycles.effects.manifest import ActionKind, EffectManifestV1
from tests.cycles.manifest_fixtures import (
    FIRST,
    HASH,
    NOW,
    SECOND,
    THIRD,
    manifest_example,
)


@pytest.fixture
def manifest() -> EffectManifestV1:
    return manifest_example()


def test_every_s7_4_field_is_present_with_normalized_machine_values(
    manifest: EffectManifestV1,
) -> None:
    data = json.loads(manifest.canonical_bytes())

    assert set(data) == {
        "schema",
        "tenant_id",
        "client_id",
        "engagement_id",
        "cycle_id",
        "cycle_revision",
        "amendment",
        "period_start",
        "period_end",
        "timezone",
        "playbook_version_id",
        "playbook_hash",
        "proposal_id",
        "proposal_revision",
        "business_intent_key",
        "action_kind",
        "adapter_version",
        "confirmation_contract_version",
        "initiator_id",
        "destination",
        "payload",
        "recipients",
        "money",
        "documents",
        "evidence",
        "observations",
        "tax_profile_versions",
        "policy",
        "authorization",
        "approval_rule",
        "not_before",
        "expires_at",
    }
    assert data["schema"] == "axorim.effect.v1"
    assert data["tenant_id"] == FIRST
    assert data["cycle_revision"] == 7
    assert data["amendment"] == 2
    assert data["period_start"] == "2026-09-01"
    assert data["period_end"] == "2026-10-01"
    assert data["timezone"] == "America/Sao_Paulo"
    assert data["playbook_hash"] == HASH
    assert data["proposal_revision"] == 3
    assert data["destination"]["account_id"] == FIRST
    assert data["recipients"][0] == {
        "role": "to",
        "contact_id": FIRST,
        "channel": "email",
        "address": "Case.Sensitive@xn--mnchen-3ya.example",
    }
    assert data["money"] == [
        {
            "name": "total",
            "decimal": "123.46",
            "currency": "BRL",
            "rounding_rule": ROUND_HALF_EVEN,
        }
    ]
    assert data["documents"][0] == {
        "document_id": FIRST,
        "sha256": HASH,
        "size": 1234,
        "intake_state": "vinculado",
        "scan_id": SECOND + ":3",
        "link_id": THIRD,
        "link_revision": 2,
        "review_state": "aceito",
    }
    assert data["tax_profile_versions"] == [FIRST, SECOND]
    assert data["authorization"]["membership_id"] == SECOND
    assert data["authorization"]["grants"][0]["expires_at"] == (
        "2026-10-03T12:30:00.000000Z"
    )
    assert data["authorization"]["grants"][1]["expires_at"] is None
    assert data["observations"][0]["observed_at"] == "2026-10-02T12:30:00.000000Z"
    assert data["expires_at"] == "2026-10-05T12:30:00.000000Z"
    assert data["not_before"] is None


def test_set_reordering_keeps_bytes_but_semantic_array_reordering_does_not(
    manifest: EffectManifestV1,
) -> None:
    reordered = replace(
        manifest,
        recipients=tuple(reversed(manifest.recipients)),
        tax_profile_versions=tuple(reversed(manifest.tax_profile_versions)),
        authorization={
            **manifest.authorization,
            "grants": tuple(reversed(manifest.authorization["grants"])),
        },
    )
    assert reordered.canonical_bytes() == manifest.canonical_bytes()
    assert reordered.digest == manifest.digest
    assert json.loads(reordered.canonical_bytes())["payload"]["ordered"] == [3, 1, 2]
    changed = replace(
        manifest, payload={"body": "Conteudo revisto", "ordered": [1, 2, 3]}
    )
    assert changed.digest != manifest.digest


def test_rebuilding_never_changes_identity_time_or_input_order(
    manifest: EffectManifestV1,
) -> None:
    original_recipients = manifest.recipients
    first = manifest.canonical_bytes()
    second = replace(manifest).canonical_bytes()

    assert first == second
    assert manifest.recipients == original_recipients
    assert manifest.recipients[0]["contact_id"] == SECOND
    assert json.loads(first)["proposal_id"] == THIRD
    assert not first.startswith(b"\xef\xbb\xbf")


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (ActionKind.CLIENT_PUBLICATION, (True, True, True, True, True)),
        (ActionKind.EMAIL_NOTICE, (True, True, True, False, True)),
        (ActionKind.CYCLE_COMPLETION, (False, True, True, True, True)),
        (ActionKind.REQUEST_WAIVER, (False, False, False, True, False)),
    ],
)
def test_only_effective_collections_participate_in_each_action(
    manifest: EffectManifestV1,
    kind: ActionKind,
    expected: tuple[bool, bool, bool, bool, bool],
) -> None:
    data = json.loads(replace(manifest, action_kind=kind).canonical_bytes())

    assert (
        tuple(
            bool(data[field])
            for field in (
                "recipients",
                "money",
                "documents",
                "evidence",
                "observations",
            )
        )
        == expected
    )


def test_floats_in_payload_are_still_rejected(manifest: EffectManifestV1) -> None:
    with pytest.raises(ValueError, match="unsupported"):
        _ = replace(manifest, payload={"amount": 1.5}).canonical_bytes()


def test_naive_timestamps_are_not_interpreted_in_server_local_time(
    manifest: EffectManifestV1,
) -> None:
    with pytest.raises(ValueError, match="timezone"):
        _ = replace(manifest, expires_at=NOW.replace(tzinfo=None)).canonical_bytes()
