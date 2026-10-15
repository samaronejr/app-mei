"""Search folds accents and case in PostgreSQL, on both sides of the match (AXR1-021).

The fold is `public.search_fold` (unaccent + lower), applied to the column and to the
query alike, so "sao" finds "São" and "SÃO" finds "sao". Typed wildcards are literal.
"""

import pytest

from apps.clients.models import ClientCompany
from apps.core.search import SearchHit, SearchKind, search
from apps.core.tenancy import tenant_context
from tests.cycles.cycle_fixtures import CycleSetup, cycle_setup
from tests.ui.factories import make_cnpj

pytestmark = pytest.mark.django_db


@pytest.fixture
def graph() -> CycleSetup:
    setup = cycle_setup("t21-accents")
    with tenant_context(setup.tenant.pk):
        ClientCompany.objects.create(
            tenant=setup.tenant,
            legal_name="Padaria São João Ltda",
            trade_name="Pão Quente",
            cnpj=make_cnpj("12ABC3450001"),
            status="active",
        )
    return setup


def _names(graph: CycleSetup, q: str) -> set[str]:
    with tenant_context(graph.tenant.pk):
        hits: list[SearchHit] = search(graph.actor, q, kinds=(SearchKind.CLIENT,))
    return {hit.label for hit in hits}


@pytest.mark.parametrize("q", ["sao", "SAO JOAO", "são joão", "pao quente", "padarai"])
def test_sao_matches_sao_with_tilde(graph: CycleSetup, q: str) -> None:
    assert _names(graph, q) == {"Padaria São João Ltda"}


def test_an_unrelated_query_finds_nothing(graph: CycleSetup) -> None:
    assert _names(graph, "ferragem") == set()


@pytest.mark.parametrize("q", ["%%", "__", "%_", "\\%", "a%"])
def test_like_wildcards_typed_by_the_user_are_literal(
    graph: CycleSetup, q: str
) -> None:
    assert _names(graph, q) == set()


@pytest.mark.parametrize("q", ["\uff05\uff05", "\uff3f\uff3f", "\uff05\uff3f"])
def test_fullwidth_wildcards_match_only_literal_characters(
    graph: CycleSetup, q: str
) -> None:
    literal = f"Literal {q}"
    with tenant_context(graph.tenant.pk):
        ClientCompany.objects.create(
            tenant=graph.tenant,
            legal_name=literal,
            cnpj=make_cnpj("34DEF5670001"),
            status="active",
        )

    assert _names(graph, q) == {literal}


def test_a_cnpj_is_found_however_it_was_punctuated(graph: CycleSetup) -> None:
    cnpj = make_cnpj("12ABC3450001")
    punctuated = f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"
    assert _names(graph, punctuated.lower()) == {"Padaria São João Ltda"}
