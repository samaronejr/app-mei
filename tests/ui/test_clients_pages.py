"""The client registry screen: who may open it, what it lists, and at what cost.

Written before the route exists, which is why **every `reverse("clients-list")` below
is called inside a test body and never at module level**. A module-level reverse would
raise `NoReverseMatch` while pytest was still collecting, and a file that cannot be
collected is broken rather than red: it reports "error" for the whole module instead of
naming which of the seven behaviours is missing.

The registry is a COLLECTION screen, and that shapes every assertion here:

* **Authorization is the capability**, and it decides 200 or 403. A role without it is
  refused outright, never handed an empty table — "no rows" and "not allowed" look
  identical on a list page, which is the disguise a permissions bug wears. Every
  refusal below is therefore asserted against a registry that demonstrably has rows for
  somebody else.
* **Portfolio scoping is `visible_clients()`**, and it decides which rows appear. A
  staff accountant holds `clients.view_all` in the published matrix and is still
  restricted to their assignments, so the scoping assertions are not implied by the
  authorization ones.
* **The tenant decides whether the screen exists at all.** On the platform host there
  is no firm, so the answer is 404 and not 403: a 403 would confirm that a registry is
  there and merely closed to this account.

The search assertions carry one trap on purpose. `12ABC34501DE35` is the RFB's official
alphanumeric CNPJ vector and `11222333000181` is a legacy numeric one; the digits of
the query `12abc` — `12` — occur inside the legacy number. So a normalizer that strips
non-digits, which is what "normalize a CNPJ" means to anyone who has not read IN RFB
nº 2.229/2024, drags the legacy client into the results and this file says so.
"""

from http import HTTPStatus
from typing import Final
from uuid import UUID

import pytest
from django.db import connection, reset_queries
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from pytest_django.fixtures import DjangoAssertNumQueries, SettingsWrapper

from apps.accounts.models import User
from apps.authz.services import Actor
from apps.clients.models import ClientCompany
from apps.core.navigation import visible_nav_items
from apps.core.templatetags.ptbr import cnpj_mask
from apps.core.tenancy import tenant_context
from apps.tenants.models import TenantRole
from tests.isolation.rolecheck import assert_isolated_role
from tests.support import totp_code
from tests.ui.factories import (
    PASSWORD,
    Firm,
    add_client,
    add_member,
    assign,
    client_base,
    make_firm,
)

pytestmark = pytest.mark.django_db(transaction=True)

# The route, the capability and the two query parameters this screen is defined by.
# Spelled out here rather than imported from the view, so that renaming one of them
# without renaming the other turns this red instead of silently agreeing with itself.
LIST_URL_NAME: Final = "clients-list"
SEARCH_PARAM: Final = "q"

# `pagina`, not `page`: apps/obligations/views.py:59 has shipped that spelling since the
# queues landed, and a second one would make the two list screens disagree about what
# `?pagina=2` means.
#
# The SIZE deliberately does not follow the queues. They page at 25 because a queue is a
# worklist — a finite pile of obligations an accountant grinds through, where a short
# page is a unit of work. The registry is a directory: it is scanned and searched rather
# than worked, so a denser page means fewer round trips to find the client you already
# know the name of. 50 per the approved plan, which states it twice — todo 11's
# implementation text ("`Paginator` 50, param `pagina`") and the §9 screen inventory
# ("paginate 50") — and the plan overrides this file wherever the two disagree.
PAGE_PARAM: Final = "pagina"
PAGE_SIZE: Final = 50

# Sized so that page one and page two are the whole set with nothing left over: the
# union assertion below is only a *complete* partition at an exact multiple.
PAGED_CLIENTS: Final = PAGE_SIZE * 2
# Straddles the page boundary, so a row can genuinely fall between the two pages.
TIED_CLIENTS: Final = PAGE_SIZE + 7
ORDERED_CLIENTS: Final = PAGE_SIZE + 7
SMALL_FIRM_CLIENTS: Final = 3

# Measured over more clients than fit on one page, so a registry that costs a query per
# row is over budget rather than merely close to it.
BUDGET_CLIENTS: Final = 60

# Measured at 60 clients, never guessed, and re-measured every time it moved. The first
# draft of this file said 25, a number nobody had counted; it became 27 by measurement,
# and 30 by measurement again when the export gate landed. All 30 are accounted for:
#
#    1-6   session, user, membership, MFA enrolment, tenant resolve, membership
#    7-9   transaction open, set_config('app.tenant_id'), SAVEPOINT
#   10-12  require_can       -> capability, membership, grant
#   13-15  portfolio_scope   -> can(view_assigned): capability, membership, grant
#   16-18  portfolio_scope   -> can(view_all):      capability, membership, grant
#      19  portfolio_scope   -> role_of, the membership read that resolves the role
#      20  Paginator COUNT(*)
#   21-23  the navigation bar's granted_levels, pinned at NAV_QUERY_BUDGET below
#   24-26  the export action's own `{% can %}` on clients.view_all, in the template:
#          capability, membership, grant
#      27  the page's 50 rows
#   28-30  RELEASE SAVEPOINT x2, AccessLog INSERT
#
# 24-26 are the newest three and they were bought on purpose. `export_clients_csv` is
# decorated `@require_can("clients.view_all")`, so an ungated button offers every
# account that cannot export a control which answers 403 — and a dead button teaches an
# accountant that the product is broken, which is a worse defect than three queries on a
# page already spending ten to answer authorization. The tag is also the cheapest way to
# ask: the answer is not lying around, because `portfolio_scope` resolves that same
# capability internally but `visible_clients` encapsulates it and the view never holds
# the result, so re-deriving it through that path would cost about seven.
#
# Thirteen of the 30 are authorization, re-resolved because `require_can`,
# `visible_clients` and the template each answer independently, and all three are
# mandatory: the capability decides 200-or-403, the portfolio decides which rows exist,
# and the template decides what is offered at all. The queryset contract forbids
# hand-writing the first two. The overhead could be removed by memoising the permission
# decision on the request, and deliberately is not —
# `tests/ui/test_dashboard.py:51-59` records that rejection for the same reason it
# applies here: this is a screen where a stale authorization answer is a visible
# cross-tenant leak.
#
# So this number is a ratchet, not the claim that protects the product. The claim that
# does is `test_the_query_count_does_not_grow_with_the_portfolio`: 30 is O(1) in
# portfolio size, and an N+1 regression moves that test rather than this one.
QUERY_BUDGET: Final = 30

# Additive only. `tests/ui/test_navigation.py` pins `OWNER_ONLY = "Equipe"` and
# `SHARED = "Exportar clientes"` and neither is touched from here: the export link is
# not replaced by the registry, it sits beside it, and a firm that loses its CSV the
# day the list ships has lost a feature it was using.
NAV_LABEL: Final = "Clientes"
NAV_EXPORT_LABEL: Final = "Exportar clientes"
NAV_QUERY_BUDGET: Final = 3

# The RFB's official alphanumeric vector, and a legacy numeric CNPJ whose digits
# overlap the query used to find the alphanumeric one. Both are checksum-valid.
ALPHANUMERIC_BASE: Final = "12ABC34501DE"
ALPHANUMERIC_CNPJ: Final = "12ABC34501DE35"
LEGACY_BASE: Final = "112223330001"
LEGACY_CNPJ: Final = "11222333000181"
# Lowercase on purpose: the column is stored uppercase, so a query that is not
# uppercased matches nothing at all.
ALPHANUMERIC_PREFIX: Final = "12abc"

ALPHANUMERIC_NAME: Final = "Alfanumérica Servicos ME"
LEGACY_NAME: Final = "Legada Transportes ME"


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]


@pytest.fixture
def alpha() -> Firm:
    """A firm with three clients, the first of them assigned to the accountant."""
    firm = make_firm("alfa-reg", client_count=SMALL_FIRM_CLIENTS)
    assign(firm, firm.clients[0], firm.accountant)
    return firm


@pytest.fixture
def busca() -> Firm:
    """A firm holding one alphanumeric and one legacy CNPJ, and nothing else."""
    firm = make_firm("busca-reg", client_count=0)
    add_client(firm, legal_name=ALPHANUMERIC_NAME, base=ALPHANUMERIC_BASE)
    add_client(firm, legal_name=LEGACY_NAME, base=LEGACY_BASE)
    return firm


def _operations_admin(firm: Firm) -> User:
    return add_member(
        firm.tenant,
        f"ops@{firm.tenant.slug}.example.com",
        TenantRole.OPERATIONS_ADMIN,
    )


def _labels(firm: Firm, user: Actor) -> list[str]:
    """Resolve the navigation bar for one account, inside this firm's context."""
    with tenant_context(firm.tenant.id):
        return [str(item.label) for item in visible_nav_items(user)]


def _body(session: Client, **params: str) -> str:
    """GET the registry with `params` and return the rendered document.

    The status gate lives here rather than in a test of its own. `pytest -k` can
    deselect a standalone sentinel, and every assertion that reads a body would then be
    scanning the text of a 403 or a stack trace and passing for the wrong reason.
    """
    response = session.get(reverse(LIST_URL_NAME), params)
    assert response.status_code == HTTPStatus.OK, (
        f"the registry answered {response.status_code} for params {params!r}, so the "
        f"document scanned below is not a rendered list"
    )
    return str(response.content.decode())


def _rendered_order(
    body: str,
    companies: list[ClientCompany],
    *,
    where: str,
) -> list[UUID]:
    """Return the pks of whichever of `companies` appear, in the order they appear.

    The non-vacuity gate is inside: a document that rendered none of the names would
    otherwise yield an empty list that compares equal to nothing and satisfies every
    "no duplicates" assertion downstream for free.
    """
    placed = [
        (body.index(company.legal_name), UUID(str(company.pk)))
        for company in companies
        if company.legal_name in body
    ]
    assert placed, (
        f"{where}: not one of the {len(companies)} client names reached the document, "
        f"so the ordering and partition assertions below would quantify over nothing"
    )
    return [pk for _, pk in sorted(placed)]


def _identified(body: str, company: ClientCompany) -> bool:
    """Report whether this client is identifiable on the page by its document.

    Either spelling counts. The stored form is what a template emits when it forgets
    the mask, and the masked form is what it emits when it remembers; insisting on one
    of them would make this a test of the punctuation rather than of the row.
    """
    return company.cnpj in body or cnpj_mask(company.cnpj) in body


def _documents_on(body: str, companies: list[ClientCompany], *, where: str) -> set[str]:
    """Return the CNPJs of whichever of `companies` are identifiable in `body`."""
    found = {company.cnpj for company in companies if _identified(body, company)}
    assert found, (
        f"{where}: none of the {len(companies)} clients is identifiable by its CNPJ — "
        f"neither stored nor masked — so rows cannot be told apart and the "
        f"duplicate/missing assertions below prove nothing"
    )
    return found


def _cost_of_one_request(firm: Firm) -> int:
    """Return the query count of a warmed-up registry request for this firm."""
    session = firm.as_owner()
    session.get(reverse(LIST_URL_NAME))
    reset_queries()
    with CaptureQueriesContext(connection) as captured:
        session.get(reverse(LIST_URL_NAME))
    return len(captured)


# --------------------------------------------------------------------- authorization


@pytest.mark.parametrize(
    "role",
    [TenantRole.OWNER, TenantRole.STAFF_ACCOUNTANT, TenantRole.OPERATIONS_ADMIN],
)
def test_the_registry_opens_for_every_firm_side_role(alpha: Firm, role: str) -> None:
    """All three firm-side roles keep books, so all three may look at the registry."""
    if role == TenantRole.OWNER:
        session = alpha.as_owner()
    elif role == TenantRole.STAFF_ACCOUNTANT:
        session = alpha.as_accountant()
    else:
        session = alpha.sign_in(_operations_admin(alpha))

    response = session.get(reverse(LIST_URL_NAME))
    assert response.status_code == HTTPStatus.OK, (
        f"{role} was answered {response.status_code} by the client registry"
    )


def test_a_portal_only_account_is_refused_rather_than_shown_an_empty_registry(
    alpha: Firm,
) -> None:
    """A client-side account has no book of business, so it has no registry.

    **There is deliberately no `can(portal, CAPABILITY) is False` assertion here, and
    re-adding one would pin a falsehood.** `clients.view_assigned` is
    `(FULL, FULL, FULL, FULL, FULL, FULL)` in the published matrix
    (`apps/authz/matrix.py:89-92`) — FULL for all six roles, client-side ones included,
    because a portal user genuinely may view the one client they are attached to. What
    separates a portal account from an accountant on THIS screen is therefore not the
    capability at all. It is two other things:

    * **host routing** — a client-role membership carries a client, so
      `TenantMiddleware`'s firm-side lookup (`client=None`) never matches it and the
      request is refused before the view is reached, which is the 403 below; and
    * **the portfolio** — `visible_clients()` is what decides which rows exist, and it
      is asserted by the scoping tests above rather than here.

    The owner's request at the bottom is the non-vacuity gate: without it the 403 could
    be passing against a registry that is empty for everybody.
    """
    url = reverse(LIST_URL_NAME)
    portal = add_member(
        alpha.tenant,
        f"portal@{alpha.tenant.slug}.example.com",
        TenantRole.CLIENT_OWNER,
        client=alpha.clients[0],
    )

    session = Client(SERVER_NAME=alpha.host)
    session.post(
        reverse("account_login"),
        {"login": portal.email, "password": PASSWORD},
    )
    session.post(reverse("mfa_authenticate"), {"code": totp_code()})
    response = session.get(url)
    assert response.status_code == HTTPStatus.FORBIDDEN, (
        f"{TenantRole.CLIENT_OWNER} was answered {response.status_code} rather than "
        f"403 by the firm-side client registry"
    )

    assert alpha.clients[0].legal_name in _body(alpha.as_owner()), (
        "the registry is empty for the owner too, so the 403 above proves nothing"
    )


def test_an_anonymous_visitor_is_sent_to_sign_in(alpha: Firm) -> None:
    response = Client(SERVER_NAME=alpha.host).get(reverse(LIST_URL_NAME))
    assert response.status_code == HTTPStatus.FOUND, (
        f"an anonymous visitor was answered {response.status_code} rather than a "
        f"redirect to the sign-in page"
    )
    assert reverse("account_login") in response.headers["Location"]


def test_the_platform_host_has_no_client_registry(alpha: Firm) -> None:
    """404, and specifically not 403: the difference is an existence oracle.

    There is no firm on the platform host, so there is no registry to be refused. A
    403 would answer "there is one and it is closed to you", which is a fact about the
    platform that an unauthenticated scan of the host must not be able to establish.
    An empty 200 would be worse still — it would state that the firm exists and keeps
    no books.
    """
    session = alpha.as_owner()
    session.defaults["SERVER_NAME"] = "localhost"
    response = session.get(reverse(LIST_URL_NAME))
    assert response.status_code == HTTPStatus.NOT_FOUND, (
        f"the platform host answered {response.status_code} for the client registry; "
        f"403 confirms the screen exists, 200 confirms a firm does"
    )


# --------------------------------------------------------------------------- scoping


def test_no_trace_of_another_firm_reaches_the_registry(alpha: Firm) -> None:
    """Authenticated as one firm, nothing of the other may reach the document.

    The positive control is the first loop rather than a test of its own: "none of
    beta's identifiers appear in alpha's registry" is worth nothing if those
    identifiers never appear in any registry, and a fixture typo or a column the
    template does not render would make the whole scan pass over the empty set.
    """
    assert_isolated_role()
    beta = make_firm("beta-reg", client_count=2)

    body = _body(alpha.as_owner())

    for company in alpha.clients:
        assert company.legal_name in body, (
            f"{company.legal_name} is missing from its own firm's registry, so the "
            f"cross-tenant scan below is searching for strings no page ever emits"
        )
        assert _identified(body, company), (
            f"{company.legal_name} is listed without its CNPJ in either spelling, so "
            f"the document assertions below can never fire"
        )

    for company in beta.clients:
        assert company.legal_name not in body, f"{company.legal_name} leaked"
        assert company.trade_name not in body, f"{company.trade_name} leaked"
        assert company.cnpj not in body, f"{company.cnpj} leaked"
        assert cnpj_mask(company.cnpj) not in body, f"{company.cnpj} leaked masked"
    assert beta.tenant.name not in body, f"{beta.tenant.name} leaked"
    assert beta.owner.email not in body, f"{beta.owner.email} leaked"
    assert beta.accountant.email not in body, f"{beta.accountant.email} leaked"


def test_a_staff_accountant_sees_only_the_clients_assigned_to_them(
    alpha: Firm,
) -> None:
    """The matrix grants staff `clients.view_all`; `visible_clients` narrows anyway."""
    body = _body(alpha.as_accountant())
    assigned = alpha.clients[0]
    assert assigned.legal_name in body, (
        f"the accountant's own assigned client {assigned.legal_name} is missing, so "
        f"the exclusions below would pass against an empty registry"
    )
    for company in alpha.clients[1:]:
        assert company.legal_name not in body, (
            f"{company.legal_name} is not assigned to the staff accountant and is "
            f"listed anyway — visible_clients was not what produced these rows"
        )


def test_an_operations_admin_sees_the_whole_firm(alpha: Firm) -> None:
    """Otherwise the narrowing above could be `visible_clients` narrowing everyone."""
    body = _body(alpha.sign_in(_operations_admin(alpha)))
    for company in alpha.clients:
        assert company.legal_name in body, (
            f"{company.legal_name} is missing for {TenantRole.OPERATIONS_ADMIN}, a "
            f"firm-wide role whose portfolio is the whole registry"
        )


# ---------------------------------------------------------------------------- search


def test_the_search_matches_a_legal_name_fragment(busca: Firm) -> None:
    body = _body(busca.as_owner(), **{SEARCH_PARAM: "Alfanum"})
    assert ALPHANUMERIC_NAME in body, (
        f"searching {SEARCH_PARAM}=Alfanum did not find {ALPHANUMERIC_NAME}, whose "
        f"legal name contains it"
    )
    assert LEGACY_NAME not in body, (
        f"searching {SEARCH_PARAM}=Alfanum returned {LEGACY_NAME}, which matches "
        f"neither its name nor its document — the filter was not applied"
    )


def test_a_lowercase_alphanumeric_cnpj_prefix_finds_only_that_client(
    busca: Firm,
) -> None:
    """The whole alphanumeric question, in one assertion pair.

    `12abc` must be uppercased to `12ABC` and compared against the stored column, which
    reaches `12ABC34501DE35` and nothing else. Strip the non-digits instead — the
    reflex a numeric-CNPJ world teaches — and the query becomes `12`, which occurs
    inside `11222333000181` at offset one. The second assertion is that mutation's
    tombstone: it is the only thing distinguishing "uppercased" from "digits only",
    because both spellings find the alphanumeric client.
    """
    assert LEGACY_CNPJ.find("12") > 0, (
        f"{LEGACY_CNPJ} no longer contains the digits of "
        f"{ALPHANUMERIC_PREFIX!r}, so this test can no longer tell an uppercasing "
        f"normalizer apart from a digit-stripping one"
    )

    body = _body(busca.as_owner(), **{SEARCH_PARAM: ALPHANUMERIC_PREFIX})

    assert ALPHANUMERIC_NAME in body, (
        f"searching {SEARCH_PARAM}={ALPHANUMERIC_PREFIX!r} did not find "
        f"{ALPHANUMERIC_NAME} ({ALPHANUMERIC_CNPJ}); the query has to be uppercased "
        f"the way the column is, per IN RFB nº 2.229/2024"
    )
    assert LEGACY_NAME not in body, (
        f"searching {SEARCH_PARAM}={ALPHANUMERIC_PREFIX!r} returned {LEGACY_NAME} "
        f"({LEGACY_CNPJ}), which shares only the digits of the query — the letters "
        f"were stripped somewhere, and a digits-only CNPJ normalizer is wrong for "
        f"every registration made from 2026-07-31 onward"
    )


@pytest.mark.parametrize("term", [None, "", "   "])
def test_an_absent_or_blank_search_lists_the_whole_portfolio(
    busca: Firm,
    term: str | None,
) -> None:
    """A blank box is not a query for nothing; it is the absence of a query.

    `ClientCompanyManager.search("")` deliberately returns `none()`, because there
    `cnpj__contains=""` would export the registry. A list screen composing that answer
    unconditionally shows an accountant an empty registry the moment they clear the
    box.
    """
    params = {} if term is None else {SEARCH_PARAM: term}
    body = _body(busca.as_owner(), **params)
    for company in busca.clients:
        assert company.legal_name in body, (
            f"{company.legal_name} is missing when {SEARCH_PARAM} is {term!r}; a "
            f"blank search term must not be applied as a filter"
        )


@pytest.mark.parametrize(
    "term",
    ["./-", ".", "%", "_", "'", '"', "\\", "%%", "a" * 512],
)
def test_an_unusable_search_term_falls_back_instead_of_failing(
    busca: Firm,
    term: str,
) -> None:
    """Typed, pasted and truncated URLs are ordinary traffic, not server errors.

    `%` and `_` are LIKE wildcards and `'` closes a string literal, so each of them is
    the shape an unescaped filter turns into either a 500 or a full-registry dump.

    Both halves are asserted, and the second is the one that bites. None of these terms
    occurs in either client's name or document, so a screen that answers 200 *and* lists
    everybody has not survived the input — it has stopped filtering on it, and the
    search box has become a one-click export of the firm's whole registry. That is the
    exact failure `ClientCompanyManager.search` guards at the manager layer and
    `tests/fiscal/test_storage_and_export.py` pins there; this lifts it to HTTP, where
    the view composes the clause.
    """
    response = busca.as_owner().get(reverse(LIST_URL_NAME), {SEARCH_PARAM: term})
    assert response.status_code == HTTPStatus.OK, (
        f"{SEARCH_PARAM}={term!r} was answered {response.status_code}; an unusable "
        f"search term is a fallback, never a failure"
    )

    body = response.content.decode()
    listed = [c.legal_name for c in busca.clients if c.legal_name in body]
    assert len(listed) < len(busca.clients), (
        f"{SEARCH_PARAM}={term!r} matches neither the name nor the document of any "
        f"client, yet all of {listed} were listed — the term was not applied, so the "
        f"search box is an accidental export of the whole registry"
    )


def test_the_search_cannot_reach_another_firms_client_by_its_cnpj(
    busca: Firm,
) -> None:
    """A search that crosses the boundary is an existence oracle for a document."""
    assert_isolated_role()
    other = make_firm("vizinha-reg", client_count=0)
    intruder = add_client(other, legal_name="Vizinha Oculta ME", base=ALPHANUMERIC_BASE)

    body = _body(busca.as_owner(), **{SEARCH_PARAM: cnpj_mask(intruder.cnpj)})

    assert ALPHANUMERIC_NAME in body, (
        f"the two firms legitimately share the CNPJ {intruder.cnpj}, so this firm's "
        f"own {ALPHANUMERIC_NAME} must still be found — an empty page would make the "
        f"exclusion below vacuous"
    )
    assert intruder.legal_name not in body, f"{intruder.legal_name} leaked via search"


# ------------------------------------------------------------------------ pagination


def test_the_first_page_is_the_first_slice_of_the_legal_name_then_pk_order(
    alpha: Firm,
) -> None:
    """Named order, not insertion order, and page one is its first `PAGE_SIZE` rows.

    The names are numbered against the creation sequence so that alphabetical order and
    primary-key order disagree everywhere. A registry ordered by `pk`, by `created_at`,
    or by nothing at all therefore renders a visibly different page one, rather than
    the same one by luck.
    """
    firm = make_firm("ordem-reg", client_count=0)
    for index in range(ORDERED_CLIENTS):
        add_client(
            firm,
            legal_name=f"Ordenada {ORDERED_CLIENTS - index - 1:03d} ME",
            base=client_base(index, firm.tenant.slug),
        )

    with tenant_context(firm.tenant.id):
        ordered = list(ClientCompany.objects.all().order_by("legal_name", "pk"))
    assert [UUID(str(c.pk)) for c in ordered] != [
        UUID(str(c.pk)) for c in firm.clients
    ], "the fixture's name order matches its creation order, so it tests nothing"

    body = _body(firm.as_owner())
    expected = [UUID(str(company.pk)) for company in ordered[:PAGE_SIZE]]

    assert _rendered_order(body, ordered, where="page one") == expected, (
        f"page one of the registry is not the first {PAGE_SIZE} clients of the "
        f'("legal_name", "pk") order, in that order'
    )
    for company in ordered[PAGE_SIZE:]:
        assert company.legal_name not in body, (
            f"{company.legal_name} sorts after position {PAGE_SIZE} and is on page "
            f"one anyway, so the page is not being sliced from a sorted queryset"
        )


def test_a_tied_legal_name_neither_repeats_nor_drops_a_row_across_pages(
    alpha: Firm,
) -> None:
    """Stability is a property of the ORDER BY, not of what the planner happens to do.

    Every client here carries the *same* legal name, so an order of `("legal_name",)`
    alone is fully tied and PostgreSQL is free to return any permutation for each
    `LIMIT/OFFSET` — it simply tends to return the same one twice in a row, which is
    why a distinct-name fixture cannot see this. An order is total exactly when it ends
    in a unique column, and only a total order guarantees that a row cannot slip
    between page one and page two and never be worked.
    """
    firm = make_firm("empate-reg", client_count=0)
    for index in range(TIED_CLIENTS):
        add_client(
            firm,
            legal_name="Homônima Comercio ME",
            base=client_base(index, firm.tenant.slug),
        )

    session = firm.as_owner()
    first = _documents_on(_body(session), firm.clients, where="page one")
    second = _documents_on(
        _body(session, **{PAGE_PARAM: "2"}),
        firm.clients,
        where="page two",
    )

    assert len(first) == PAGE_SIZE, (
        f"page one carries {len(first)} of the {TIED_CLIENTS} identically named "
        f"clients, not {PAGE_SIZE}"
    )
    assert first & second == set(), (
        f"{sorted(first & second)} appear on both page one and page two, so the order "
        f"is not total and the same client is worked twice"
    )
    assert first | second == {company.cnpj for company in firm.clients}, (
        f"{sorted({c.cnpj for c in firm.clients} - (first | second))} appear on "
        f"neither page, so a client fell between them and is never worked"
    )


@pytest.mark.parametrize("page", ["999", "0", "-1", "abc", "", "2.5", "1e3"])
def test_an_unusable_page_number_falls_back_to_the_first_page(
    alpha: Firm,
    page: str,
) -> None:
    """Out of range, negative, or not a number at all: all of them are page one."""
    body = _body(alpha.as_owner(), **{PAGE_PARAM: page})
    assert alpha.clients[0].legal_name in body, (
        f"{PAGE_PARAM}={page!r} produced a page that does not carry "
        f"{alpha.clients[0].legal_name}; an unusable page number falls back to the "
        f"first page rather than to an empty one"
    )


def test_page_one_and_page_two_together_are_the_whole_ordered_set(
    alpha: Firm,
) -> None:
    """Exactly two full pages, so the union is the registry with nothing left over."""
    firm = make_firm("paginas-reg", client_count=PAGED_CLIENTS)
    with tenant_context(firm.tenant.id):
        ordered = list(ClientCompany.objects.all().order_by("legal_name", "pk"))
    expected = [UUID(str(company.pk)) for company in ordered]
    assert len(expected) == PAGED_CLIENTS

    session = firm.as_owner()
    first = _rendered_order(_body(session), ordered, where="page one")
    second = _rendered_order(
        _body(session, **{PAGE_PARAM: "2"}),
        ordered,
        where="page two",
    )

    assert len(first) == PAGE_SIZE, f"page one holds {len(first)} rows, not {PAGE_SIZE}"
    assert set(first) & set(second) == set(), (
        f"{len(set(first) & set(second))} client(s) appear on both pages"
    )
    assert first + second == expected, (
        f'page one followed by page two is not the ("legal_name", "pk") order of the '
        f"{PAGED_CLIENTS} clients: "
        f"{len(set(expected) - set(first + second))} missing, "
        f"{len(set(first + second) - set(expected))} unexpected"
    )


# ------------------------------------------------------------------------------ cost


def test_the_registry_stays_inside_its_query_budget(
    django_assert_max_num_queries: DjangoAssertNumQueries,
) -> None:
    """A screen listing the whole book of business is the one that must stay cheap."""
    firm = make_firm("custo-reg", client_count=BUDGET_CLIENTS)
    session = firm.as_owner()
    session.get(reverse(LIST_URL_NAME))
    with django_assert_max_num_queries(QUERY_BUDGET):
        session.get(reverse(LIST_URL_NAME))


def test_the_query_count_does_not_grow_with_the_portfolio(
    alpha: Firm,
    django_assert_max_num_queries: DjangoAssertNumQueries,
) -> None:
    """Fifty clients must cost what three cost, or the first real firm times out."""
    small = _cost_of_one_request(alpha)
    assert small, "the small firm's registry issued no queries at all"

    big = make_firm("grande-reg", client_count=PAGED_CLIENTS)
    session = big.as_owner()
    session.get(reverse(LIST_URL_NAME))
    with django_assert_max_num_queries(small):
        session.get(reverse(LIST_URL_NAME))


# ------------------------------------------------------------------------ navigation


def test_the_bar_now_offers_the_registry_to_the_owner_and_to_staff(
    alpha: Firm,
) -> None:
    """Additive: the registry joins the bar, the CSV export keeps its place on it."""
    reverse(LIST_URL_NAME)
    for who, user in (("owner", alpha.owner), ("staff accountant", alpha.accountant)):
        labels = _labels(alpha, user)
        assert labels, (
            f"the {who}'s nav bar resolved to nothing at all, so neither of the two "
            f"membership checks below is being made against a real bar"
        )
        assert NAV_LABEL in labels, (
            f"the {who} is not offered {NAV_LABEL!r}, though the registry is the "
            f"screen their whole day is spent on"
        )
        assert NAV_EXPORT_LABEL in labels, (
            f"the {who} lost {NAV_EXPORT_LABEL!r} when {NAV_LABEL!r} arrived; the "
            f"registry does not replace the CSV export, it sits beside it"
        )


def test_the_registry_item_did_not_raise_the_bars_query_budget(
    alpha: Firm,
    django_assert_num_queries: DjangoAssertNumQueries,
) -> None:
    """One query per item would make the bar the most expensive thing on the page."""
    assert NAV_LABEL in _labels(alpha, alpha.owner), (
        f"{NAV_LABEL!r} is not in the bar at all, so measuring the bar's cost says "
        f"nothing about what adding it cost"
    )
    with tenant_context(alpha.tenant.id), django_assert_num_queries(NAV_QUERY_BUDGET):
        visible_nav_items(alpha.owner)


def test_the_rendered_bar_links_to_the_registry_without_dropping_the_export(
    alpha: Firm,
) -> None:
    """Resolved in a real document, because a label is not a working link."""
    registry = reverse(LIST_URL_NAME)
    export = reverse("clients-export-csv")
    body = alpha.as_accountant().get(reverse("dashboard")).content.decode()

    assert f'href="{export}"' in body, (
        f"the staff accountant's page no longer links to {export}, so this document "
        f"is not the bar this test is about"
    )
    assert f'href="{registry}"' in body, (
        f"the staff accountant's rendered bar carries no link to {registry}"
    )


def test_the_registry_is_not_streamed(alpha: Firm) -> None:
    """`TenantMiddleware` refuses a streaming response, and the reason is not style.

    Its iterator runs after the tenant transaction has closed, so every lazy query
    inside it returns zero rows under the fail-closed policy — a registry that renders
    perfectly and lists nobody.
    """
    response = alpha.as_owner().get(reverse(LIST_URL_NAME))
    assert not response.streaming


def test_the_registry_renders_a_whole_page(alpha: Firm) -> None:
    body = _body(alpha.as_owner())
    assert body.lstrip().startswith("<!DOCTYPE html>")
    assert "<main" in body
