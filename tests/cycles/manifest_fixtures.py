"""Pure synthetic manifest inputs shared by the byte and matrix contracts."""

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal

from apps.cycles.effects.manifest import ActionKind, EffectManifestV1, MonetaryEffect

FIRST = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SECOND = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
THIRD = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
HASH = "ab" * 32
NOW = datetime(2026, 10, 2, 12, 30, tzinfo=UTC)


def manifest_example() -> EffectManifestV1:
    return EffectManifestV1(
        tenant_id=FIRST.upper(),
        client_id=SECOND,
        engagement_id=THIRD,
        cycle_id=FIRST,
        cycle_revision=7,
        amendment=2,
        period_start=date(2026, 9, 1),
        period_end=date(2026, 10, 1),
        playbook_version_id=SECOND,
        playbook_hash=HASH.upper(),
        proposal_id=THIRD,
        proposal_revision=3,
        business_intent_key="cd" * 32,
        action_kind=ActionKind.CLIENT_PUBLICATION,
        adapter_version="publication.v1",
        confirmation_contract_version="publication-record.v1",
        initiator_id=FIRST,
        destination={
            "provider": "portal",
            "account_id": FIRST.upper(),
            "operation": "publish",
        },
        payload={"body": "Conteudo revisto", "ordered": [3, 1, 2]},
        recipients=(
            {
                "role": "to",
                "contact_id": SECOND,
                "channel": "email",
                "address": "Second@example.com",
            },
            {
                "role": "to",
                "contact_id": FIRST.upper(),
                "channel": "email",
                "address": "Case.Sensitive@M\u00fcnchen.EXAMPLE",
            },
        ),
        money=(MonetaryEffect("total", Decimal("123.455"), "brl", 2, ROUND_HALF_EVEN),),
        documents=(
            {
                "document_id": FIRST.upper(),
                "sha256": HASH.upper(),
                "size": 1234,
                "intake_state": "vinculado",
                "scan_id": SECOND.upper() + ":3",
                "link_id": THIRD.upper(),
                "link_revision": 2,
                "review_state": "aceito",
            },
        ),
        evidence=({"request_id": FIRST, "revision": 4, "decision_id": SECOND},),
        observations=(
            {
                "id": THIRD,
                "source_kind": "official_api",
                "status": "verified",
                "observed_at": NOW,
                "valid_until": NOW + timedelta(days=7),
            },
        ),
        tax_profile_versions=(SECOND.upper(), FIRST),
        policy={"id": FIRST, "version": 4, "hash": HASH.upper()},
        authorization={
            "membership_id": SECOND.upper(),
            "membership_version": 6,
            "grants": (
                {"id": THIRD, "version": 2, "expires_at": NOW + timedelta(days=1)},
                {"id": FIRST, "version": 1, "expires_at": None},
            ),
        },
        approval_rule={
            "required_capability": "actions.approve",
            "quorum": 2,
            "separation_of_duties": True,
        },
        not_before=None,
        expires_at=datetime(2026, 10, 5, 9, 30, tzinfo=timezone(timedelta(hours=-3))),
    )
