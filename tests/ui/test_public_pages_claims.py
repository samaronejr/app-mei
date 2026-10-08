"""Public and help copy makes no claim the product cannot back.

`docs/product/vocabulary.md` says public copy makes no absolute claims: no "tempo
real", no "100%", no compliance guarantee, and no delivery word for a message the
SMTP relay only accepted. The pages under `templates/public/` and `templates/help/`
are the copy a prospect or a client reads first, so they are scanned in full, raw
source included: a phrase inside a comment is one edit away from the page.

The scan also holds three launch decisions in place until their owner decisions close:
no price amount (OD-07, "Fale conosco"), no public "Axorim" name (OD-01), and the
pricing page saying "Fale conosco". The render half checks the routes really serve
these templates, and that the public pages stay off a firm's subdomain.
"""

import re
from http import HTTPStatus
from pathlib import Path
from typing import Final

import pytest
from django.test import Client
from django.urls import reverse

from apps.core.public_views import HELP_PAGES
from tests.ui.factories import make_firm

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
SCANNED_DIRS: Final = (
    PROJECT_ROOT / "templates" / "public",
    PROJECT_ROOT / "templates" / "help",
)

FORBIDDEN_CLAIMS: Final[dict[str, re.Pattern[str]]] = {
    "100% seguro": re.compile(r"100\s*%\s*segur[oa]", re.IGNORECASE),
    "tempo real": re.compile(r"tempo\s+real", re.IGNORECASE),
    "entregue": re.compile(r"\bentregues?\b", re.IGNORECASE),
    "conformidade garantida": re.compile(r"conformidade\s+garantida", re.IGNORECASE),
    "sem erros": re.compile(r"\bsem\s+erros?\b", re.IGNORECASE),
}
PRICE_AMOUNT: Final = re.compile(r"R\$\s*\d")
UNCLEARED_NAME: Final = re.compile(r"axorim", re.IGNORECASE)

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
