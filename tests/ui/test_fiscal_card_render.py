"""A frozen received-NF-e effect renders on both approval cards without a link."""

import re
from uuid import UUID

import pytest
from django.template.loader import render_to_string
from django.urls import Resolver404, resolve, reverse
from django.utils.html import strip_tags

from apps.cycles.effects.manifest import JSONValue
from tests.cycles.manifest_fixtures import FIRST, SECOND
from tests.cycles.test_fiscal_manifestation_action import _manifest
from tests.integrations.capture_fixtures import key

ORDINARY_LINK = "01a21c00-0000-7000-8000-0000000000d2"
ORDINARY: dict[str, JSONValue] = {
    "document_id": "01a21c00-0000-7000-8000-0000000000d1",
    "link_id": ORDINARY_LINK,
    "link_revision": 1,
    "sha256": "0" * 64,
    "size": 10,
    "content_type": "application/pdf",
    "obligation_id": None,
}


def _review_links(rendered: str) -> list[str]:
    """Every anchor that resolves to the document review page (not its link text)."""
    found = []
    for href in re.findall(r'href="([^"#?]+)', rendered):
        try:
            match = resolve(href)
        except Resolver404:
            continue
        if match.url_name == "document-review":
            found.append(href)
    return found


@pytest.mark.parametrize(
    "template",
    ["cycles/approval_card.html", "cycles/_bulk_effect.html"],
    ids=["approval-card", "bulk-effect"],
)
def test_a_fiscal_vector_renders_its_keys_and_label_without_a_review_link(
    template: str,
) -> None:
    manifest = _manifest()
    frozen = manifest.as_data()
    # An ordinary frozen document beside the fiscal ones keeps its review link.
    documents = frozen["documents"]
    assert isinstance(documents, list)
    frozen["documents"] = [*documents, ORDINARY]
    context = {
        "proposal": {
            "pk": FIRST,
            "cycle_id": SECOND,
            "revision": 1,
            "digest": manifest.digest,
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
                "proposal_digest": manifest.digest,
            },
        },
    }
    rendered: object
    try:
        rendered = render_to_string(template, context)
    except Exception as error:  # noqa: BLE001 - the outcome is the assertion
        rendered = error
    assert isinstance(rendered, str), rendered
    visible = strip_tags(rendered).casefold()
    assert "focus · ciência da operação" in visible
    # The approval card shows each key twice: in the list and in its technical details.
    times = 2 if template == "cycles/approval_card.html" else 1
    assert all(visible.count(f"nf-e {key(index)}") == times for index in (1, 2))
    assert _review_links(rendered) == [
        reverse("document-review", args=[UUID(ORDINARY_LINK)])
    ]
    if template == "cycles/_bulk_effect.html":
        assert "registrar ciencia (lote)" in visible
