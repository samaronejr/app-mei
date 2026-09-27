"""Test-only pages for the browser suite: the component kit and planted defects.

Served by `tests/browser/urls.py`, which the browser conftest installs as
`ROOT_URLCONF` for browser tests alone. The templates live beside this module,
outside every template directory the project scans, so a planted defect here can
never reach a shipped page or trip a guard over shipped templates.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from django.http import HttpRequest, HttpResponse
from django.template import engines
from django.template.backends.django import DjangoTemplates
from django.template.base import Template

TEMPLATES: Final[Path] = Path(__file__).resolve().parent / "templates"

STATES: Final = (
    "confirmed",
    "manual",
    "pending",
    "unknown",
    "stale",
    "blocked",
    "danger",
)

ROW_TEMPLATE: Final = (
    "<tr><td>{{ row.client }}</td><td>{{ row.period }}</td>"
    '<td>{% include "partials/ui/status_badge.html" with state=row.state '
    "label=row.state_label only %}</td>"
    '<td class="numeric">{{ row.amount }}</td></tr>'
)
DIALOG_BODY: Final = (
    "<p>O e-mail com a guia DAS de março vai para "
    "<strong>financeiro@padaria.example.com</strong>.</p>"
)
DRAWER_BODY: Final = (
    '<dl class="grid grid-cols-2 gap-2 text-sm"><dt class="text-ink-600">Arquivo</dt>'
    '<dd>nota-0001.xml</dd><dt class="text-ink-600">Recebido</dt>'
    "<dd>05/04/2026 09:12</dd></dl>"
)


def _engine_template(source: str) -> Template:
    """Compile with the project's engine, so `{% include %}` accepts the result."""
    backend = engines["django"]
    assert isinstance(backend, DjangoTemplates)
    return backend.engine.from_string(source)


def _render(name: str, request: HttpRequest, context: dict[str, Any]) -> HttpResponse:
    source = (TEMPLATES / name).read_text(encoding="utf-8")
    template = engines["django"].from_string(source)
    return HttpResponse(template.render(context, request))


def component_kit(request: HttpRequest) -> HttpResponse:
    """Render every `partials/ui/` component with realistic pt-BR data."""
    checked_at = datetime(2026, 4, 5, 11, 2, tzinfo=UTC)
    context: dict[str, Any] = {
        "states": [*STATES, "nao-mapeado"],
        "filters": [
            {
                "name": "q",
                "field_id": "kit-busca",
                "label": "Buscar cliente",
                "type": "search",
                "value": "",
            },
            {
                "name": "situacao",
                "field_id": "kit-situacao",
                "label": "Situação",
                "type": "select",
                "value": "pending",
                "options": [
                    {"value": "", "label": "Todas"},
                    {"value": "pending", "label": "Pendente"},
                    {"value": "confirmed", "label": "Confirmado"},
                ],
            },
        ],
        "columns": [
            {"label": "Cliente", "sort": "ascending", "sort_url": "?ordem=-cliente"},
            {"label": "Competência"},
            {"label": "Situação"},
            {"label": "Valor", "numeric": True},
        ],
        "rows": [
            {
                "client": "Padaria Pão Quente LTDA",
                "period": "03/2026",
                "state": "confirmed",
                "state_label": "Confirmado",
                "amount": "R$ 75,90",
            },
            {
                "client": "Oficina Duas Rodas MEI",
                "period": "03/2026",
                "state": "manual",
                "state_label": "Pagamento informado",
                "amount": "R$ 75,90",
            },
            {
                "client": "Studio Ana Beleza",
                "period": "03/2026",
                "state": "stale",
                "state_label": "Desatualizado",
                "amount": "R$ 81,90",
            },
        ],
        "no_rows": [],
        "row_template": _engine_template(ROW_TEMPLATE),
        "received": [
            {
                "name": "extrato-marco.pdf",
                "size_label": "184 KB",
                "state": "confirmed",
                "state_label": "Recebido",
            },
            {
                "name": "nota-0001.xml",
                "size_label": "12 KB",
                "state": "pending",
                "state_label": "Em verificação",
            },
        ],
        "effects": [
            {
                "label": "Enviar e-mail para financeiro@padaria.example.com",
                "detail": "Assunto: Guia DAS de março/2026 · 1 anexo, 84 KB",
            },
            {
                "label": "Publicar a guia no portal da Padaria Pão Quente",
                "detail": "Visível para 2 pessoas da empresa",
            },
        ],
        "evidence": [{"label": "Guia DAS 03/2026 (PDF)", "url": "/__ui__/kit/"}],
        "dialog_body": _engine_template(DIALOG_BODY),
        "drawer_body": _engine_template(DRAWER_BODY),
        "timeline": [
            {
                "when": datetime(2026, 4, 5, 12, 12, tzinfo=UTC),
                "summary": "E-mail aceito pelo servidor",
                "actor": "Envio automático",
                "state": "pending",
                "state_label": "Aguardando leitura",
            },
            {
                "when": datetime(2026, 4, 4, 18, 30, tzinfo=UTC),
                "summary": "Guia aprovada para envio",
                "actor": "Ana",
                "state": "confirmed",
                "state_label": "Aprovado",
                "href": "/__ui__/kit/",
                "href_label": "Ver aprovação",
            },
        ],
        "checked_at": checked_at,
        # `?gaveta=1` is the drawer reached by its own URL (S5.6 `hx-push-url`).
        "drawer_open": request.GET.get("gaveta") == "1",
    }
    return _render("component_kit.html", request, context)


def swap_fragment(request: HttpRequest) -> HttpResponse:
    """The fragment the kit's HTMX button swaps in, carrying a focus marker."""
    return _render("swap_fragment.html", request, {})


def unnamed_button(request: HttpRequest) -> HttpResponse:
    """A page with one control that has no accessible name, for axe to refuse."""
    return _render("unnamed_button.html", request, {})
