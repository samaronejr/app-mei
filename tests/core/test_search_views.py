"""The firm search endpoint: a page without script, a listbox fragment for HTMX."""

import pytest
from pytest_django import DjangoAssertNumQueries

from tests.portal.request_fixtures import PortalSetup, portal_setup

pytestmark = pytest.mark.django_db(transaction=True)

# Measured, every line fixed per request rather than per hit: session, user,
# membership, MFA and tenant resolution (6); the tenant transaction (BEGIN,
# set_config, SAVEPOINT, RELEASE, COMMIT: 5); require_can (3); the portfolio scope
# (3); cycles.review for the documents kind (3); one query per kind (4); the access
# log INSERT (1). The searched term matches several rows per kind, so a per-hit
# lookup would break this.
SEARCH_QUERY_BUDGET = 25


@pytest.fixture
def setup() -> PortalSetup:
    return portal_setup("t21-views")


def test_without_script_the_header_form_lands_on_a_results_page(
    setup: PortalSetup,
) -> None:
    response = setup.firm().get("/busca/", {"q": "Synthetic"})

    assert response.status_code == 200
    body = response.content.decode()
    assert '<form role="search"' in body
    assert f'href="/clientes/{setup.graph.client.pk}/"' in body
    assert f'href="/ciclos/{setup.cycle.pk}/"' in body
    assert 'role="listbox"' not in body


def test_htmx_receives_only_the_listbox_fragment(setup: PortalSetup) -> None:
    response = setup.firm().get("/busca/", {"q": "Synthetic"}, HTTP_HX_REQUEST="true")

    assert response.status_code == 200
    body = response.content.decode()
    assert "<html" not in body
    assert 'role="listbox"' in body
    assert f'data-href="/clientes/{setup.graph.client.pk}/"' in body


def test_a_short_query_renders_a_prompt_not_results(setup: PortalSetup) -> None:
    body = (
        setup.firm().get("/busca/", {"q": "S"}, HTTP_HX_REQUEST="true").content.decode()
    )

    assert 'role="option"' not in body
    assert "Digite ao menos 2 caracteres" in body


def test_the_search_stays_inside_its_query_budget(
    setup: PortalSetup,
    django_assert_max_num_queries: DjangoAssertNumQueries,
) -> None:
    browser = setup.firm()
    browser.get("/busca/", {"q": "client"}, HTTP_HX_REQUEST="true")

    with django_assert_max_num_queries(SEARCH_QUERY_BUDGET):
        response = browser.get("/busca/", {"q": "client"}, HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    body = response.content.decode()
    assert body.count('data-kind="client"') == 2
    assert body.count('data-kind="cycle"') == 2


@pytest.mark.parametrize("q", ["Syn\x00thetic", "\x00\x00", "a" * 5000])
def test_hostile_input_is_answered_not_raised(setup: PortalSetup, q: str) -> None:
    response = setup.firm().get("/busca/", {"q": q}, HTTP_HX_REQUEST="true")

    assert response.status_code == 200


def test_only_get_is_answered(setup: PortalSetup) -> None:
    assert setup.firm().post("/busca/", {"q": "Synthetic"}).status_code == 405
