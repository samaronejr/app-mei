"""The `templates/partials/ui/` contracts a test client can check without a browser.

Behaviour that needs a browser -- focus, Alpine, axe -- is exercised in
`tests/browser/test_components.py`. What is pinned here is the markup contract each
partial documents in its own header: the status badge's three signals, the plain
field's ARIA chain, the table's sort and empty semantics.
"""

import re
from pathlib import Path
from typing import Any, Final

import pytest
from django.conf import settings
from django.template.loader import render_to_string

from tests.ui.test_pure_partials import CAPABILITY_LIBRARY

UI_PARTIALS: Final[Path] = Path(settings.BASE_DIR) / "templates" / "partials" / "ui"

# The partial set the plan names (S5.6 / todo 3), by file.
REQUIRED: Final = frozenset(
    {
        "dialog.html",
        "drawer.html",
        "effects_card.html",
        "field.html",
        "filter_bar.html",
        "state_empty.html",
        "state_error.html",
        "state_stale.html",
        "status_badge.html",
        "table.html",
        "timeline_item.html",
        "toast.html",
        "upload_list.html",
    },
)

# Evidence state -> the pt-BR word the badge falls back to (S5.7 vocabulary).
STATE_WORDS: Final = {
    "confirmed": "Confirmado",
    "manual": "Informado (não confirmado)",
    "pending": "Pendente",
    "unknown": "Sem informação",
    "stale": "Desatualizado",
    "blocked": "Bloqueado",
    "danger": "Falhou",
}


def _render(name: str, context: dict[str, Any]) -> str:
    return render_to_string(f"partials/ui/{name}", context)


def test_every_named_partial_exists() -> None:
    found = {path.name for path in UI_PARTIALS.glob("*.html")}
    assert sorted(REQUIRED - found) == []


def test_no_ui_partial_loads_a_capability_tag_library() -> None:
    """Partials render from context; a capability lookup belongs to the view."""
    sources = sorted(UI_PARTIALS.glob("*.html"))
    assert len(sources) >= len(REQUIRED)
    offenders = [
        path.name
        for path in sources
        if CAPABILITY_LIBRARY.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


@pytest.mark.parametrize(("state", "word"), sorted(STATE_WORDS.items()))
def test_a_status_badge_is_a_hue_a_glyph_and_a_word(state: str, word: str) -> None:
    html = _render("status_badge.html", {"state": state})

    assert f"status-{state}" in html
    assert f'data-state="{state}"' in html
    assert html.count("<svg") == 1
    assert 'aria-hidden="true"' in html
    assert f"<span>{word}</span>" in html


def test_green_is_spent_on_confirmed_alone() -> None:
    painted = [
        state
        for state in STATE_WORDS
        if "status-confirmed" in _render("status_badge.html", {"state": state})
    ]
    assert painted == ["confirmed"]


def test_an_unrecognised_state_renders_as_unknown_never_as_itself() -> None:
    html = _render("status_badge.html", {"state": "paid", "label": ""})

    assert "status-unknown" in html
    assert 'data-state="unknown"' in html
    assert "paid" not in html
    assert "<span>Sem informação</span>" in html


def test_a_badge_label_replaces_the_vocabulary_word() -> None:
    html = _render(
        "status_badge.html",
        {"state": "manual", "label": "Pagamento informado"},
    )
    assert "<span>Pagamento informado</span>" in html


def test_a_field_with_errors_is_invalid_and_names_help_and_error() -> None:
    html = _render(
        "field.html",
        {
            "name": "q",
            "field_id": "busca",
            "label": "Buscar",
            "help_text": "Nome ou CNPJ.",
            "errors": ["Digite ao menos 3 letras."],
        },
    )

    assert '<label class="ui-field-label" for="busca">Buscar' in html
    assert 'aria-invalid="true"' in html
    assert 'aria-describedby="busca_help busca_error"' in html
    assert 'id="busca_help"' in html
    assert 'id="busca_error"' in html


def test_a_clean_field_carries_neither_aria_invalid_nor_an_empty_reference() -> None:
    html = _render("field.html", {"name": "q", "field_id": "busca", "label": "Buscar"})

    assert "aria-invalid" not in html
    assert "aria-describedby" not in html


def test_a_select_marks_the_current_option() -> None:
    html = _render(
        "field.html",
        {
            "name": "situacao",
            "field_id": "situacao",
            "label": "Situação",
            "type": "select",
            "value": "pending",
            "options": [
                {"value": "", "label": "Todas"},
                {"value": "pending", "label": "Pendente"},
            ],
        },
    )
    assert '<option value="pending" selected>Pendente</option>' in html
    assert '<option value="">Todas</option>' in html


def test_a_table_sets_aria_sort_only_for_a_real_direction() -> None:
    html = _render(
        "table.html",
        {
            "caption": "Ciclos",
            "columns": [
                {"label": "Cliente", "sort": "ascending", "sort_url": "?o=-c"},
                {"label": "Valor", "sort": "sideways", "numeric": True},
            ],
            "rows": [],
            "empty_title": "Nenhum ciclo.",
        },
    )

    assert html.count("aria-sort=") == 1
    assert 'aria-sort="ascending"' in html
    assert "<caption>Ciclos</caption>" in html
    assert 'role="region" aria-label="Ciclos"' in html


def test_an_empty_table_renders_the_empty_state_across_every_column() -> None:
    html = _render(
        "table.html",
        {
            "caption": "Ciclos",
            "columns": [{"label": "A"}, {"label": "B"}, {"label": "C"}],
            "rows": [],
            "empty_title": "Nenhum ciclo.",
        },
    )

    assert 'colspan="3"' in html
    assert 'data-ui-state="empty"' in html
    assert "Nenhum ciclo." in html


def test_an_error_state_interrupts_and_offers_a_plain_retry() -> None:
    html = _render("state_error.html", {"retry_url": "/clientes/"})

    assert 'role="alert"' in html
    assert re.search(r'<a class="[^"]*" href="/clientes/">Tentar novamente</a>', html)


def test_a_toast_carries_no_live_region_of_its_own() -> None:
    """The shell's permanent #toasts region announces it; a second would repeat."""
    html = _render("toast.html", {"message": "Salvo.", "variant": "success"})

    assert "aria-live" not in html
    assert "hx-swap-oob" not in html
    oob = _render("toast.html", {"message": "Salvo.", "oob": True})
    assert oob.startswith('<div hx-swap-oob="beforeend:#toasts">')
