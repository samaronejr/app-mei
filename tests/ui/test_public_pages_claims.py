"""Public and help copy makes no claim the product cannot back.

`docs/product/vocabulary.md` says public copy makes no absolute claims: no "tempo
real", no "100%", no compliance guarantee, and no delivery word for a message the
SMTP relay only accepted. The pages under `templates/public/` and `templates/help/`
are the copy a prospect or a client reads first, so they are scanned in full, raw
source included: a phrase inside a comment is one edit away from the page.

The scan also holds three launch decisions in place until their owner decisions close:
no price amount (OD-07, "Fale conosco"), no public "Axorim" name (OD-01), and the
pricing page saying "Fale conosco". The render half checks the routes really serve
these templates, and that the public pages stay off a firm's subdomain. It also reads
the copy as rendered (AXRM-051), because a page's title and summary come from
`apps/core/public_views.py`, not from its template.
"""

import re
from dataclasses import replace
from datetime import date
from html.parser import HTMLParser
from http import HTTPStatus
from pathlib import Path
from typing import Final

import pytest
from django.test import Client
from django.urls import reverse
from django.utils.translation import gettext_lazy

from apps.core import public_views
from apps.core.public_views import HELP_PAGES
from tests.ui.factories import make_firm

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
SCANNED_DIRS: Final = (
    PROJECT_ROOT / "templates" / "public",
    PROJECT_ROOT / "templates" / "help",
)

OVERBROAD_CLAIMS: Final[dict[str, re.Pattern[str]]] = {
    # IS-4: only consequential exact-effect actions need an approval card;
    # templated notices, reminders and invitations send under policy/playbook.
    "approval before any send": re.compile(
        r"antes\s+de\s+qualquer\s+envio|"
        r"nada\s+sai\s+para\s+o\s+cliente\s+sem[^.]*|"
        r"tudo\s+o\s+que\s+sai\s+para\s+o\s+cliente\s+passa\s+por\s+uma\s+aprova|"
        r"sem\s+algu[ée]m\s+aprovar\s+exatamente\s+o\s+que\s+vai\s+sair",
        re.IGNORECASE,
    ),
    # S15 B: only extractable identifiers raise divergence; images/scans are
    # "não verificável" and need the firm's manual review (no OCR in R1).
    "any other-company document becomes an exception": re.compile(
        r"(?<!leg[íi]veis,\s)um\s+documento\s+de\s+outra\s+empresa[^.]*vira\s+uma\s+pend[êe]ncia|"
        r"(?<!leg[íi]veis,\s)um\s+documento[^.]*cnpj\s+diferente[^.]*vira\s+uma\s+pend[êe]ncia",
        re.IGNORECASE,
    ),
}

FORBIDDEN_CLAIMS: Final[dict[str, re.Pattern[str]]] = {
    "100% seguro": re.compile(r"100\s*%\s*segur[oa]", re.IGNORECASE),
    "tempo real": re.compile(r"tempo\s+real", re.IGNORECASE),
    "entregue": re.compile(r"\bentregues?\b", re.IGNORECASE),
    "conformidade garantida": re.compile(r"conformidade\s+garantida", re.IGNORECASE),
    "sem erros": re.compile(r"\bsem\s+erros?\b", re.IGNORECASE),
}
REGIONAL_CLAIMS: Final[dict[str, re.Pattern[str]]] = {
    # The regional forbidden wording of docs/product/vocabulary.md (AXRM-051).
    "regular": re.compile(r"\bregular(?:es)?\b", re.IGNORECASE),
    "em dia": re.compile(r"\bem\s+dia\b", re.IGNORECASE),
    "confirmado pela prefeitura": re.compile(
        r"\bconfirmad[oa]s?\s+pela\s+prefeitura\b", re.IGNORECASE
    ),
    "100%": re.compile(r"\b100\s*%"),
}
FEDERAL_STAGE_PAGES: Final = ("consultas-a-receita", "o-que-nao-faz")
PRICE_AMOUNT: Final = re.compile(r"R\$\s*\d")
UNCLEARED_NAME: Final = re.compile(r"axorim", re.IGNORECASE)
# One rendered text per page (title, view-supplied summaries and body) carries every
# AXRM-051 check; the R1 source scans below stay as they were.
RENDERED_CLAIMS: Final = {
    **FORBIDDEN_CLAIMS,
    **REGIONAL_CLAIMS,
    "price amount before OD-07": PRICE_AMOUNT,
    "public name before OD-01": UNCLEARED_NAME,
}
SEM_PENDENCIAS: Final = re.compile(r"\bsem\s+pend[êe]ncias?\b", re.IGNORECASE)
DATED_SOURCES: Final = re.compile(
    r"\s+nas\s+fontes\s+verificadas\s+em\s+(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\b",
    re.IGNORECASE,
)


def _real_date(day: str, month: str, year: str | None) -> bool:
    try:
        # 2000 is a leap year, so a 29/02 written without its year is a real date.
        date(int(year or 2000), int(month), int(day))
    except ValueError:
        return False
    return True


def undated_sem_pendencias(text: str) -> bool:
    """Tell whether "sem pendências" appears without its sources and a real date."""
    for found in SEM_PENDENCIAS.finditer(text):
        dated = DATED_SOURCES.match(text, found.end())
        if dated is None or not _real_date(*dated.groups()):
            return True
    return False


PUBLIC_ROUTES: Final = ("public-product", "public-pricing")


def _templates() -> list[Path]:
    return sorted(path for root in SCANNED_DIRS for path in root.rglob("*.html"))


def _hits(pattern: re.Pattern[str], paths: list[Path]) -> list[str]:
    found: list[str] = []
    for path in paths:
        location = path.relative_to(PROJECT_ROOT)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                found.append(f"{location}:{number}: {line.strip()}")
    return found


class _VisibleText(HTMLParser):
    """The text a reader gets: every data node outside script and style."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._hidden:
            self._hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            self.parts.append(data)


def _rendered(client: Client, name: str) -> str:
    response = client.get(reverse(name))
    assert response.status_code == HTTPStatus.OK, name
    reader = _VisibleText()
    reader.feed(response.content.decode())
    return " ".join(" ".join(reader.parts).split())


def _rendered_hits() -> list[str]:
    client = Client(SERVER_NAME="localhost")
    names = ["help-index", *(page.url_name for page in HELP_PAGES), *PUBLIC_ROUTES]
    hits: list[str] = []
    for name in names:
        text = _rendered(client, name)
        hits += [
            f"{name}: {phrase}"
            for phrase, pattern in RENDERED_CLAIMS.items()
            if pattern.search(text)
        ]
        if undated_sem_pendencias(text):
            hits.append(f"{name}: sem pendências")
    return hits


def test_the_scan_covers_every_public_and_help_page() -> None:
    names = {path.relative_to(PROJECT_ROOT).as_posix() for path in _templates()}
    expected = {
        "templates/public/produto.html",
        "templates/public/precos.html",
        "templates/help/index.html",
        *(f"templates/{page.template}" for page in HELP_PAGES),
    }
    assert expected <= names, sorted(expected - names)


@pytest.mark.parametrize("phrase", sorted(FORBIDDEN_CLAIMS))
def test_no_public_or_help_template_makes_a_forbidden_claim(phrase: str) -> None:
    hits = _hits(FORBIDDEN_CLAIMS[phrase], _templates())
    assert hits == [], f"forbidden claim {phrase!r}:\n" + "\n".join(hits)


@pytest.mark.parametrize(
    ("phrase", "planted"),
    [
        ("100% seguro", "Seus dados 100 % seguros."),
        ("tempo real", "Acompanhe em Tempo  Real."),
        ("entregue", "Mensagem entregue ao cliente."),
        ("conformidade garantida", "Conformidade garantida com a Receita."),
        ("sem erros", "Fechamento sem erros."),
    ],
)
def test_each_detector_finds_its_planted_phrase(phrase: str, planted: str) -> None:
    assert FORBIDDEN_CLAIMS[phrase].search(planted), planted


@pytest.mark.django_db
def test_no_rendered_public_or_help_page_makes_a_forbidden_claim() -> None:
    hits = _rendered_hits()
    assert hits == [], "forbidden claim in rendered copy:\n" + "\n".join(hits)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("summary", "hit"),
    [
        ("Atestamos que a empresa regular nada deve.", "regular"),
        ("Tudo em dia com o fisco.", "em dia"),
        ("ISS confirmado pela prefeitura.", "confirmado pela prefeitura"),
        ("Cobertura de 100% das notas.", "100%"),
        ("A partir de R$ 390 por mês.", "price amount before OD-07"),
        ("Sem pendências.", "sem pendências"),
        ("Sem pendências nas fontes verificadas em breve.", "sem pendências"),
        ("Sem pendências nas fontes verificadas em 31/02/2026.", "sem pendências"),
        (
            "Sem pendências nas fontes verificadas em {{ emitted_on }}.",
            "sem pendências",
        ),
    ],
)
def test_a_view_supplied_summary_is_read_as_rendered(
    monkeypatch: pytest.MonkeyPatch, summary: str, hit: str
) -> None:
    first, *rest = HELP_PAGES
    planted = replace(first, summary=gettext_lazy(summary))
    monkeypatch.setattr(public_views, "HELP_PAGES", (planted, *rest))
    assert f"help-index: {hit}" in _rendered_hits()


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        ("Sem pendências nas fontes verificadas em 03/11/2026.", False),
        ("Sem pendências nas fontes verificadas em 29/02.", False),
        ("Sem pendências nas fontes verificadas em 29/02/2027.", True),
        ("Sem pendências nas fontes verificadas em 03/13.", True),
        ("Regularize o cadastro; sem pendências nas fontes.", True),
    ],
)
def test_sem_pendencias_needs_its_sources_and_a_real_date(
    text: str, flagged: bool
) -> None:
    assert undated_sem_pendencias(text) is flagged, text
    assert not REGIONAL_CLAIMS["regular"].search("Regularize o cadastro.")


@pytest.mark.django_db
def test_the_help_index_lists_the_federal_stage_pages() -> None:
    slugs = {page.slug for page in HELP_PAGES}
    assert set(FEDERAL_STAGE_PAGES) <= slugs, sorted(set(FEDERAL_STAGE_PAGES) - slugs)
    body = Client(SERVER_NAME="localhost").get(reverse("help-index")).content.decode()
    for slug in FEDERAL_STAGE_PAGES:
        assert f'href="{reverse(f"help-{slug}")}"' in body, slug


def test_no_price_amount_is_published_before_od_07() -> None:
    hits = _hits(PRICE_AMOUNT, _templates())
    assert hits == [], "price amount before OD-07:\n" + "\n".join(hits)
    assert PRICE_AMOUNT.search("a partir de R$ 390")


def test_the_pricing_page_says_fale_conosco() -> None:
    pricing = PROJECT_ROOT / "templates" / "public" / "precos.html"
    assert "Fale conosco" in pricing.read_text(encoding="utf-8")


def test_no_public_name_is_used_before_od_01() -> None:
    hits = _hits(UNCLEARED_NAME, _templates())
    assert hits == [], "public name before OD-01:\n" + "\n".join(hits)


@pytest.mark.django_db
def test_every_help_page_renders_on_the_apex_and_on_a_firm_host() -> None:
    firm = make_firm("ajuda-claims")
    names = ["help-index", *(page.url_name for page in HELP_PAGES)]
    for host in ("localhost", firm.host):
        client = Client(SERVER_NAME=host)
        for name in names:
            response = client.get(reverse(name))
            assert response.status_code == HTTPStatus.OK, (host, name)


@pytest.mark.django_db
def test_public_pages_render_on_the_apex_only() -> None:
    firm = make_firm("publico-claims")
    apex = Client(SERVER_NAME="localhost")
    firm_host = Client(SERVER_NAME=firm.host)
    for name in PUBLIC_ROUTES:
        response = apex.get(reverse(name))
        assert response.status_code == HTTPStatus.OK, name
        assert "app-mei" in response.content.decode()
        assert firm_host.get(reverse(name)).status_code == HTTPStatus.NOT_FOUND, name


@pytest.mark.parametrize("phrase", sorted(OVERBROAD_CLAIMS))
def test_no_template_overstates_approval_or_detection(phrase: str) -> None:
    hits = _hits(OVERBROAD_CLAIMS[phrase], _templates())
    assert hits == [], f"overbroad claim {phrase!r}:\n" + "\n".join(hits)


def test_the_approval_blurb_states_the_templated_exception() -> None:
    source = (PROJECT_ROOT / "apps" / "core" / "public_views.py").read_text(
        encoding="utf-8"
    )
    assert "aviso" in source, "approval blurb must name the templated sends"
