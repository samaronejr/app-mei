"""Customer-facing destinations do not expose frozen internal identifiers."""

from dataclasses import replace

import pytest
from django.template.loader import render_to_string
from django.utils.html import strip_tags

from apps.cycles.effects.manifest import ActionKind
from tests.cycles.manifest_fixtures import FIRST, NOW, SECOND, manifest_example


@pytest.mark.parametrize("kind", list(ActionKind))
@pytest.mark.parametrize(
    "template", ["cycles/approval_card.html", "cycles/_bulk_effect.html"]
)
def test_cards_keep_internal_destination_ids_in_the_manifest(
    kind: ActionKind, template: str
) -> None:
    # Given a frozen effect using the actual builder's internal destination IDs.
    provider = "smtp" if kind is ActionKind.EMAIL_NOTICE else "axorim"
    manifest = replace(
        manifest_example(),
        action_kind=kind,
        destination={
            "provider": provider,
            "account_id": FIRST,
            "operation": kind.value,
        },
    )
    original_bytes = manifest.canonical_bytes()
    original_digest = manifest.digest
    frozen = manifest.as_data()
    context = {
        "proposal": {
            "pk": FIRST,
            "cycle_id": SECOND,
            "revision": 3,
            "digest": original_digest,
            "client": {"legal_name": "Empresa de teste", "cnpj": "12345678000195"},
        },
        "action_label": "Ação de teste",
        "frozen": frozen,
        "dates": {"expires_at": manifest.expires_at},
        "kind": "approve_items",
        "item": {
            "subject_ref": FIRST,
            "effect": {
                "frozen": frozen,
                "action_label": "Ação de teste",
                "proposal_digest": original_digest,
            },
        },
    }

    # When either shipped card renders the same frozen effect.
    visible = strip_tags(render_to_string(template, context)).casefold()

    # Then presentation hides the internal IDs without rewriting the effect.
    assert provider not in visible
    assert kind.value not in visible
    destination = render_to_string(
        "cycles/_effect_destination.html", {"action_kind": kind}
    ).strip()
    assert destination
    assert destination.casefold() in visible
    assert manifest.canonical_bytes() == original_bytes
    assert manifest.digest == original_digest
    assert frozen == manifest.as_data()


def test_each_effect_kind_has_a_distinct_nonempty_destination() -> None:
    # Given all supported frozen effect kinds.
    # When the shared customer-facing destination fragment renders each one.
    labels = {
        render_to_string(
            "cycles/_effect_destination.html", {"action_kind": kind}
        ).strip()
        for kind in ActionKind
    }

    # Then no kind loses its destination or becomes indistinguishable.
    assert "" not in labels
    assert len(labels) == len(ActionKind)


def test_bulk_quota_does_not_publish_the_uncleared_product_name() -> None:
    # Given the notices preview, which displays the current daily quota.
    context = {
        "preview": True,
        "operation": {
            "kind": "send_request_notices",
            "created_at": NOW,
            "valid_until": NOW,
            "snapshot_json": {"estimated_messages": 0},
        },
        "remaining_quota": 100,
        "rows": [],
    }

    # When the actual bulk page renders its quota explanation.
    visible = strip_tags(
        render_to_string("cycles/bulk_detail.html", context)
    ).casefold()

    # Then this default customer surface does not pre-empt the naming decision.
    assert "axorim" not in visible
