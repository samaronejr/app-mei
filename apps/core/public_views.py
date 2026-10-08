"""The help centre at `/ajuda/` and the two public product pages.

Every page here is static copy: no query, no form, no tenant data. The help pages are
served on any host that mounts the root URLconf, so a firm's staff read them on their
own subdomain and a prospect reads them on the apex. The product and pricing pages are
apex-only: on a firm's subdomain they 404, because a firm's address is that firm's
workspace and not a sales page.

Every route is static, one `path()` per page, so no dynamic parameter reaches a
template name. `tests/ui/test_public_pages_claims.py` keeps the copy honest.
"""

from dataclasses import dataclass
from http import HTTPStatus
from typing import Final

from django.conf import settings
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import URLPattern, path
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_GET


@dataclass(frozen=True, slots=True)
class HelpPage:
    """One help article: its URL slug, its title and its one-line summary."""

    slug: str
    title: Promise
    summary: Promise

    @property
    def url_name(self) -> str:
        """The route name `reverse()` takes for this page."""
        return f"help-{self.slug}"

    @property
    def template(self) -> str:
        """The template that holds this page's copy."""
        return f"help/{self.slug}.html"


HELP_PAGES: Final[tuple[HelpPage, ...]] = (
    HelpPage(
        "comece-aqui",
        _("Comece aqui"),
        _("O caminho do primeiro mês, da importação ao ciclo concluído."),
    ),
    HelpPage(
        "importar-clientes",
        _("Importar clientes"),
        _("Planilha, prévia, confirmação e como desfazer."),
    ),
    HelpPage(
        "ciclos",
        _("Ciclos do mês"),
        _("Como um ciclo abre, o que ele pede e quando termina."),
    ),
    HelpPage(
        "aprovacoes",
        _("Aprovações"),
        _("Nada sai para o cliente sem alguém aprovar exatamente o que vai sair."),
    ),
    HelpPage(
        "aceita-pelo-servidor",
        _("O que quer dizer \u201caceita pelo servidor\u201d"),
        _("O que sabemos, e o que não sabemos, depois que um e-mail sai."),
    ),
    HelpPage(
        "cobertura",
        _("Cobertura das obrigações"),
        _("Sem informação, desatualizado, informado e verificado não se misturam."),
    ),
    HelpPage(
        "portal-do-cliente",
        _("Guia do portal para clientes"),
        _("Para a empresa cliente: como responder às solicitações da contabilidade."),
    ),
    HelpPage(
        "acesso-de-suporte",
        _("Acesso de suporte"),
        _("Como o suporte pode ajudar sem acesso invisível aos seus dados."),
    ),
    HelpPage(
        "encerramento-da-conta",
        _("Encerramento e exportação"),
        _("Como sair levando os seus dados, e o que acontece depois."),
    ),
)


def _context(**extra: object) -> dict[str, object]:
    """Context every page here shares: the product label and the help index."""
    return {"product_name": settings.PRODUCT_NAME, "help_pages": HELP_PAGES, **extra}


@require_GET
def help_index_view(request: HttpRequest) -> HttpResponse:
    """List every help article."""
    return render(request, "help/index.html", _context(), status=HTTPStatus.OK)


@require_GET
def help_page_view(request: HttpRequest, *, page: HelpPage) -> HttpResponse:
    """Render one help article."""
    return render(request, page.template, _context(page=page), status=HTTPStatus.OK)


def _refuse_firm_host(request: HttpRequest) -> None:
    """404 on a firm's subdomain: the public pages belong to the apex only.

    `resolved_tenant` is routing, set for anonymous visitors too, which is exactly
    the question asked here. It is never used to scope data.
    """
    if getattr(request, "resolved_tenant", None) is not None:
        raise Http404


@require_GET
def product_view(request: HttpRequest) -> HttpResponse:
    """Render the public product page: what ships today, and what it won't promise."""
    _refuse_firm_host(request)
    return render(request, "public/produto.html", _context(), status=HTTPStatus.OK)


@require_GET
def pricing_view(request: HttpRequest) -> HttpResponse:
    """Render the public pricing page: "Fale conosco" until OD-07 sets a price."""
    _refuse_firm_host(request)
    return render(request, "public/precos.html", _context(), status=HTTPStatus.OK)


urlpatterns: list[URLPattern] = [
    path("ajuda/", help_index_view, name="help-index"),
    *(
        path(f"ajuda/{page.slug}/", help_page_view, {"page": page}, name=page.url_name)
        for page in HELP_PAGES
    ),
    path("produto/", product_view, name="public-product"),
    path("precos/", pricing_view, name="public-pricing"),
]

__all__ = [
    "HELP_PAGES",
    "HelpPage",
    "help_index_view",
    "help_page_view",
    "pricing_view",
    "product_view",
    "urlpatterns",
]
