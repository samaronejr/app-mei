"""The product shell behind `cycles_r1`, and the MEI-era shell everywhere else.

Two promises, asserted from both sides. A firm carrying the flag gets the product
shell: the `PRODUCT_NAME` wordmark and the S5.3 destinations its capabilities
allow. Every other firm, and every anonymous or platform page, gets exactly the
header it had -- the shell is chosen in `templates/base.html` by one tag that
answers None for all of them.

The browser half (keyboard, axe, both viewports) is `tests/browser/test_shell.py`;
this module holds what a test client can prove without a browser.
"""

import re
from http import HTTPStatus

import pytest
from django.db import connection
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.core.navigation import NAV_R1, PRODUCT_SHELL_FLAG, product_shell
from tests.ui.factories import Firm, make_firm

pytestmark = pytest.mark.django_db(transaction=True)

SHELL_MARKER = 'data-shell="r1"'
LEGACY_DISCLOSURE = "nav-disclosure"
SCRIPT_SRC = re.compile(r'<script src="[^"]*js/([\w.-]+\.js)"')


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]


def _flag(firm: Firm) -> Firm:
    firm.tenant.features = [PRODUCT_SHELL_FLAG]
    firm.tenant.save(update_fields=["features"])
    return firm


def _dashboard(firm: Firm, *, accountant: bool = False) -> str:
    client = firm.as_accountant() if accountant else firm.as_owner()
    response = client.get(reverse("dashboard"))
    assert response.status_code == HTTPStatus.OK
    return response.content.decode()


def _nav_labels(body: str) -> list[str]:
    nav = re.search(r"<nav\b[^>]*>(.*?)</nav>", body, re.DOTALL)
    assert nav is not None, "the page draws no <nav>"
    return [
        " ".join(label.split())
        for label in re.findall(r"<a\b[^>]*>(.*?)</a>", nav.group(1), re.DOTALL)
    ]


def test_every_product_shell_destination_resolves() -> None:
    assert NAV_R1, "the flagged tuple is empty"
    for item in NAV_R1:
        assert item.url().startswith("/"), item.url_name


def test_a_flag_off_firm_is_served_the_mei_era_shell() -> None:
    body = _dashboard(make_firm("shell-off"))

    assert SHELL_MARKER not in body
    assert "shell-r1" not in body
    assert LEGACY_DISCLOSURE in body
    # MEI-era order: Alpine before app.js, exactly as before the flag existed.
    assert SCRIPT_SRC.findall(body) == ["htmx.min.js", "alpine-csp.min.js", "app.js"]


def test_a_flagged_firm_gets_the_product_shell_named_by_the_setting(
    settings: SettingsWrapper,
) -> None:
    settings.PRODUCT_NAME = "Nome Provisório"
    body = _dashboard(_flag(make_firm("shell-on")))

    assert SHELL_MARKER in body
    assert '<body class="min-h-screen shell-r1">' in body
    assert LEGACY_DISCLOSURE not in body
    assert ">Nome Provisório</a>" in body
    assert _nav_labels(body) == [
        "Hoje",
        "Clientes",
        "Ciclos",
        "Documentos",
        "Comunicação",
        "Playbooks",
        "Gestão",
    ]
    assert re.search(
        r'<a class="shell-link" href="/" aria-current="page">Hoje</a>', body
    )
    # Alpine after app.js, so the components are registered before it starts.
    assert SCRIPT_SRC.findall(body) == ["htmx.min.js", "app.js", "alpine-csp.min.js"]


def test_the_wordmark_defaults_to_app_mei() -> None:
    body = _dashboard(_flag(make_firm("shell-nome")))
    assert '<a class="shell-wordmark" href="/">app-mei</a>' in body


def test_a_staff_accountant_under_the_flag_is_not_offered_gestao() -> None:
    body = _dashboard(_flag(make_firm("shell-staff")), accountant=True)
    assert _nav_labels(body) == [
        "Hoje",
        "Clientes",
        "Ciclos",
        "Documentos",
        "Comunicação",
    ]


def test_the_kill_switch_returns_a_flagged_firm_to_the_mei_era_shell(
    settings: SettingsWrapper,
) -> None:
    settings.AXORIM_DISABLED_FEATURES = [PRODUCT_SHELL_FLAG]
    body = _dashboard(_flag(make_firm("shell-kill")))

    assert SHELL_MARKER not in body
    assert LEGACY_DISCLOSURE in body


def test_the_shell_costs_no_query_beyond_the_mei_era_one() -> None:
    """The flag is answered from the request's primed cache, the bar costs the same."""
    plain = make_firm("custo-a")
    flagged = _flag(make_firm("custo-b"))

    def cost(firm: Firm) -> int:
        session = firm.as_owner()
        with CaptureQueriesContext(connection) as captured:
            assert session.get(reverse("dashboard")).status_code == HTTPStatus.OK
        return len(captured.captured_queries)

    assert cost(flagged) == cost(plain)


def test_no_request_and_no_granted_tenant_both_mean_no_shell() -> None:
    assert product_shell(None) is None
    anonymous = RequestFactory().get("/")
    assert product_shell(anonymous) is None
    html = render_to_string("base.html")
    assert SHELL_MARKER not in html
    assert "<main" in html
