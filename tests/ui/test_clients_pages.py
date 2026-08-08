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
from apps.fiscal.capabilities import (
    CapabilityStatus,
    capability_for,
    unknown_capability,
)
from apps.fiscal.models import Municipality
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
# 30 by measurement again when the export gate landed, and 27 once more when
# `portfolio_scope` stopped asking the same three tables twice. All 27 are accounted
# for:
#
#    1-6   session, user, membership, MFA enrolment, tenant resolve, membership
#    7-9   transaction open, set_config('app.tenant_id'), SAVEPOINT
#   10-12  require_can       -> capability, membership, grant
#   13-15  portfolio_scope   -> granted_levels(view_assigned, view_all): capability,
#                              membership, grant — ONE set of three for BOTH questions
#      16  portfolio_scope   -> role_of, the membership read that resolves the role
#      17  Paginator COUNT(*)
#   18-20  the navigation bar's granted_levels, pinned at NAV_QUERY_BUDGET below
#   21-23  the export action's own `{% can %}` on clients.view_all, in the template:
#          capability, membership, grant
#      24  the page's 50 rows
#   25-27  RELEASE SAVEPOINT, COMMIT, AccessLog INSERT
#
# The last three read "RELEASE SAVEPOINT x2" until this was re-measured statement by
# statement; the second of them is the COMMIT. Corrected here rather than left alone,
# because an itemisation that does not match the trace is how the total drifts from the
# reasons for it.
#
# 21-23 are the newest three and they were bought on purpose. `export_clients_csv` is
# decorated `@require_can("clients.view_all")`, so an ungated button offers every
# account that cannot export a control which answers 403 — and a dead button teaches an
# accountant that the product is broken, which is a worse defect than three queries on a
# page already spending seven to answer authorization. The tag is also the cheapest way
# to ask: the answer is not lying around, because `portfolio_scope` resolves that same
# capability internally but `visible_clients` encapsulates it and the view never holds
# the result, so re-deriving it through that path would cost about four.
#
# Ten of the 27 are authorization, re-resolved because `require_can`,
# `visible_clients` and the template each answer independently, and all three are
# mandatory: the capability decides 200-or-403, the portfolio decides which rows exist,
# and the template decides what is offered at all. The queryset contract forbids
# hand-writing the first two. The overhead could be removed by memoising the permission
# decision on the request, and deliberately is not —
# `tests/ui/test_dashboard.py:51-59` records that rejection for the same reason it
# applies here: this is a screen where a stale authorization answer is a visible
# cross-tenant leak.
#
# Three came off at 13-16, and they came off WITHOUT touching that rejection. What was
# 13-19 was `portfolio_scope` asking `can()` twice — capability, membership, grant, then
# the same three again for the second capability — plus `role_of`. It now asks
# `granted_levels` for both capabilities at once, which is the same resolution
# `resolve_level` performs (`test_bulk_resolution_agrees_with_resolve_level` walks every
# capability against every role to keep the two from drifting), so seven became four:
# one capability read, one membership read, one grant read, and `role_of` on the branch
# where both answers were yes.
#
# The distinction that makes this legal is WHOSE answer is reused. Nothing is memoised
# and nothing is shared: `require_can` at 10-12 and the template's `{% can %}` at 21-23
# still go to the database on their own, and still would if this file demanded it. Only
# the two questions inside `portfolio_scope`'s single answer were merged, and that
# answer is still read fresh on every call. A stale answer is still impossible, which is
# the property the rejection above exists to protect — not the query count.
#
# `apps/authz/portfolio.py` compares `full` explicitly rather than truthily, and
# `tests/authz/test_portfolio.py` sweeps all forty-nine (view_assigned, view_all) level
# pairs against every role to hold it there. That test earns its keep: with the
# comparison loosened to "anything but none", an operations_admin holding `view_all` at
# `limited` is scoped ALL instead of ASSIGNED — a portfolio widened past its matrix
# ceiling — and every assertion made against the matrix AS SEEDED still passes, because
# no role holds either of these two capabilities at that level today.
#
# So this number is a ratchet, not the claim that protects the product. The claim that
# does is `test_the_query_count_does_not_grow_with_the_portfolio`: 27 is O(1) in
# portfolio size, and an N+1 regression moves that test rather than this one. That test
# measures the small firm and compares the large one against what it measured, so it
# never needed editing here — which is the point of it being the load-bearing one.
QUERY_BUDGET: Final = 27

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


# ------------------------------------------------------- the detail screen's contract
#
# The detail screen is a SINGLETON, and every constant below exists because a singleton
# fails differently from the collection above it.
#
# A list page leaks by rendering a row. A detail page leaks by answering a *status*:
# the URL carries the client's primary key, so the response code alone is an oracle.
# 403 says "this id names a real client and you may not read it"; 404 says nothing at
# all. Both feel like a refusal to whoever wrote them, and only one of them is. That is
# why the two refusal tests below assert `== NOT_FOUND` rather than `!= OK`, and why
# each of them proves the record genuinely exists — through a second account that can
# read it — before claiming the 404 was confinement rather than an empty database.
DETAIL_URL_NAME: Final = "client-detail"

# Sixty, and the ceiling is only half the claim. A max-assert is satisfied at 20 and at
# 60 alike, so it cannot distinguish a screen that got slower from one that was always
# cheap — which is why `_detail_cost` measures with `CaptureQueriesContext` and reports
# the number it actually saw, and why `test_the_detail_query_count_does_not_grow_with_
# the_portfolio` pins the shape rather than the size.
#
# The list screen next door is measured at QUERY_BUDGET = 30 against 60 clients, and its
# breakdown at lines 96-134 is the floor this budget is built on: the detail screen runs
# the same middleware, the same `require_can`, the same `visible_clients` and the same
# navigation bar, so roughly 26 of those 30 are structural and unavoidable here too.
# What replaces the list's `Paginator COUNT(*)` and 50-row page is one scoped row read
# plus one `select_related("capability")` municipality lookup. Sixty therefore leaves
# about thirty queries of headroom for whatever the screen goes on to show — enough for
# assignments, obligations and documents, and nowhere near enough to hide an N+1.
DETAIL_QUERY_BUDGET: Final = 60

# Two real municipalities, neither of them a state capital, so neither is seeded by
# `apps/fiscal/migrations/0002_seed_capitals.py`. They are not asserted to be unknown by
# assumption: each test calls `capability_for` on its own code first and fails there if
# a future seed ever makes the fixture known, rather than silently testing the KNOWN
# path while claiming to test the UNKNOWN one.
ABSENT_IBGE: Final = "3548500"  # Santos/SP — not in the municipality table at all.
UNASSESSED_IBGE: Final = "3552205"  # Sorocaba/SP — in the table, never assessed.

# The two halves of the UNKNOWN contract at `apps/fiscal/capabilities.py:68-117`, quoted
# rather than paraphrased. `locale/` holds no compiled catalogue, so `gettext_lazy`
# returns its own msgid and these are literally the bytes a template emits.
#
# They are spelled out here AND compared against the module that produces them, in the
# same assertion. Only the literal can catch a template that stops rendering the note;
# only the comparison can catch a literal that drifted from the source. Importing alone
# would let the copy be rewritten into something reassuring with this file still green.
ABSENT_DISCLAIMER: Final = (
    "This municipality is not in the capability registry. The values shown are "
    "conservative defaults, not verified facts."
)
UNASSESSED_DISCLAIMER: Final = (
    "This municipality is registered but its capabilities have not been assessed. "
    "The values shown are conservative defaults."
)


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


# ----------------------------------------------- from the registry into one client
#
# The bridge between the collection screen and the singleton below it, and the one
# direction nothing above ever asserted. `templates/clients/detail.html:28-30` renders a
# breadcrumb back to the registry, so a client's page has always been able to reach the
# list; the list could not reach a client, because it spelled the razão social as plain
# text and the only anchor on the whole page was the CSV export.
#
# That defect ships green past every other assertion in this file, which is why it
# shipped. Each of them asks whether a name reached the document — `legal_name in body`
# — and a name rendered as inert text satisfies every one of them. Not one asked whether
# the name was *reachable*, so a directory that cannot be walked into passed as a
# working directory, on the single screen whose entire purpose is finding somebody.
#
# The href is compared literally against `reverse(DETAIL_URL_NAME, args=[pk])` rather
# than pattern-matched for "some link", because both ways this breaks leave the page
# answering 200: the `{% url ... as var %}` form swallows a NoReverseMatch and leaves
# `href=""` behind, and a row wired to the wrong primary key is a perfectly working link
# to somebody else's client.

# The msgid of the column the client's name lives in, and the `data-label` its cell
# carries. Compared as the msgid because `locale/` holds no compiled catalogue — the
# same property the two disclaimer literals above are asserted on.
NAME_CELL_LABEL: Final = "Razão social"


def _detail_href(company: ClientCompany) -> str:
    """Return the exact href attribute the registry must carry for this client."""
    return f'href="{reverse(DETAIL_URL_NAME, args=[company.pk])}"'


def _name_cell(body: str, company: ClientCompany) -> str:
    """Return the razão social cell naming this client, sliced from its label.

    Sliced from the `data-label` rather than from the name, so that what is returned is
    provably the *labelled* cell. Below the stacking breakpoint the header row is hidden
    and each cell regrows its column heading out of that attribute — the treatment
    `tests/ui/test_responsive_structure.py` guards — so a link introduced by moving the
    name into a cell of its own, or by trading the label away to make room for the
    anchor, leaves the razão social unlabelled on a phone. Both fail here rather than on
    somebody's handset.

    The count is asserted at exactly one, which is also this helper's non-vacuity gate:
    zero means the row never rendered, and every caller's link assertion would otherwise
    be reading an empty string that contains no href and no name either.
    """
    marker = f'data-label="{NAME_CELL_LABEL}"'
    cells = [
        cell.split("</td>")[0]
        for cell in body.split(marker)[1:]
        if company.legal_name in cell.split("</td>")[0]
    ]
    assert len(cells) == 1, (
        f"{company.legal_name} appears inside {len(cells)} {marker} cells rather than "
        f"exactly one, so the registry either never rendered the row, dropped the "
        f"label the phone layout rebuilds the column heading from, or listed the "
        f"client twice"
    )
    return cells[0]


def test_every_listed_client_links_to_its_own_detail_screen(alpha: Firm) -> None:
    """Every row reaches the client it names, and reaches that client specifically.

    The registry is a directory, and the only thing an accountant does with a directory
    is open something out of it. A row that names a client without linking to one is
    therefore not a cosmetic omission — it is the screen failing at the single job it
    exists for, while every status code, every scoping assertion and every query budget
    in this file stays green.

    Two non-vacuity gates, both required and neither implied by the other. The first
    fails if the fixture seeded nobody, so the loop cannot pass by having no rows to
    check. The second fails if the *document* carries fewer rows than the firm has
    clients, so a registry that rendered an empty table — or quietly lost half of it —
    cannot satisfy a loop that only ever visits the rows it can already see.
    """
    assert alpha.clients, (
        "the fixture seeded no clients at all, so there is no row for the registry to "
        "link and the loop below would quantify over nothing"
    )

    body = _body(alpha.as_owner())
    listed = [company for company in alpha.clients if company.legal_name in body]
    assert len(listed) == len(alpha.clients), (
        f"the owner's registry rendered {len(listed)} of the firm's "
        f"{len(alpha.clients)} clients; the loop below only visits rows that reached "
        f"the document, so a short — or empty — row set would let it pass without "
        f"ever looking at a single link"
    )

    for company in listed:
        assert _detail_href(company) in _name_cell(body, company), (
            f"{company.legal_name} is listed without {_detail_href(company)}, so the "
            f"registry names a client it offers no way of opening — the screen an "
            f"accountant browses to find somebody cannot reach them"
        )


def test_the_link_the_registry_renders_opens_the_client_it_names(alpha: Firm) -> None:
    """Followed out of the document, rather than merely matched inside it.

    The test above proves the registry emits the right string. This one proves the
    string is a working route for this session and lands on this client: it reads the
    href back off the rendered cell, requests exactly that, and checks who answered.

    The empty-href assertion is the reason this is a separate test. An href that failed
    to reverse is not an error anywhere — the tag resolved through an `as` variable
    returns the empty string, the page still answers 200, the row still draws and reads
    as a link, and clicking it silently reloads the registry. Nothing in this file could
    see that before, because the string `href=""` is present in a document just as
    surely as a real one is.

    The closing loop is what separates "links somewhere" from "links here": a row wired
    to a neighbouring primary key opens a real client, at a real URL, under a real 200.
    """
    session = alpha.as_owner()
    body = _body(session)
    company = alpha.clients[0]
    cell = _name_cell(body, company)

    opening = 'href="'
    assert opening in cell, (
        f"{company.legal_name}'s cell carries no href at all, so there is no link to "
        f"follow and nothing below is being measured against a navigable row"
    )
    href = cell.split(opening)[1].split('"')[0]
    assert href, (
        f"{company.legal_name} is rendered as a link with an empty href — what the url "
        f"tag leaves behind when it is resolved through an `as` variable that failed "
        f"to reverse. The page answers 200, the row looks navigable, and following it "
        f"reloads the registry"
    )

    followed = session.get(href)
    assert followed.status_code == HTTPStatus.OK, (
        f"the link the registry rendered for {company.legal_name} answered "
        f"{followed.status_code}; the row is drawn as navigable and is not"
    )
    opened = followed.content.decode()
    assert company.legal_name in opened, (
        f"following {company.legal_name}'s own row reached a page that never names "
        f"them, so the registry links its rows somewhere other than their own client"
    )
    for other in alpha.clients[1:]:
        assert other.legal_name not in opened, (
            f"following {company.legal_name}'s row opened a page naming "
            f"{other.legal_name}; the rows carry one another's primary keys, which is "
            f"a working link to the wrong client's file"
        )


# ----------------------------------------------------------------------- detail screen


def _detail_body(session: Client, company: ClientCompany) -> str:
    """GET one client's detail page and return the document it rendered.

    Both gates live in here rather than in tests of their own. `pytest -k` can deselect
    a standalone sentinel, and every refusal assertion below would then be comparing a
    404 against a screen that answers 404 to everybody — which reads as airtight
    confinement and is in fact a broken route.
    """
    response = session.get(reverse(DETAIL_URL_NAME, args=[company.pk]))
    assert response.status_code == HTTPStatus.OK, (
        f"the detail screen answered {response.status_code} for {company.legal_name}, "
        f"so the document scanned below is not a rendered client"
    )
    body = str(response.content.decode())
    assert company.legal_name in body, (
        f"the detail screen answered 200 for {company.legal_name} without naming it "
        f"anywhere, so this is not that client's page and anything asserted against "
        f"it is a statement about some other document"
    )
    return body


def _refusal(session: Client, company: ClientCompany) -> tuple[int, str]:
    """GET a detail page expecting to be turned away, and report exactly how."""
    response = session.get(reverse(DETAIL_URL_NAME, args=[company.pk]))
    return response.status_code, str(response.content.decode())


def _detail_cost(firm: Firm, company: ClientCompany) -> int:
    """Return the MEASURED query count of a warmed-up detail request.

    Counted with `CaptureQueriesContext`, never bounded with a max-assert. A ceiling of
    60 is satisfied by a screen costing 20 and by one costing 60, so it cannot tell a
    budget with headroom from a budget already spent — and it is satisfied outright by
    a 404, which costs almost nothing at all. `_detail_body` renders the page before
    the meter starts, so the number returned is the cost of a real document and the
    caller can put it in its own failure message.
    """
    session = firm.as_owner()
    _detail_body(session, company)
    reset_queries()
    with CaptureQueriesContext(connection) as captured:
        session.get(reverse(DETAIL_URL_NAME, args=[company.pk]))
    return len(captured)


def _set_municipality(firm: Firm, company: ClientCompany, ibge_code: str) -> None:
    """Point a client at a municipality, in its own tenant context."""
    with tenant_context(firm.tenant.id):
        ClientCompany.objects.filter(pk=company.pk).update(
            municipality_ibge_code=ibge_code,
        )
    company.municipality_ibge_code = ibge_code


def test_another_firms_client_is_not_found_rather_than_refused(alpha: Firm) -> None:
    """A neighbouring firm's primary key must answer 404, and nothing else.

    Resolved out of `visible_clients()` and never through
    `get_object_or_404(ClientCompany, pk=...)`. The manager scopes that lookup to the
    tenant, so it would also fail — but it would fail as a *lookup*, and the
    distinction survives the first refactor that reaches for `.all()` or for an
    unscoped `objects` manager. Selecting from the portfolio makes the confinement a
    property of the queryset the screen is built on rather than of a filter someone
    remembered to write. `_selected_client` at `apps/core/dashboard.py:91-113` resolves
    the dashboard's calendar the same way and for the same reason.

    Both gates matter and neither is a test of its own. Alpha's own client proves the
    screen renders for this session; beta's own owner proves the row is real and
    renderable, so the 404 below is confinement rather than a primary key that names
    nothing.
    """
    assert_isolated_role()
    beta = make_firm("beta-det", client_count=2)
    intruder = beta.clients[0]

    session = alpha.as_owner()
    _detail_body(session, alpha.clients[0])
    _detail_body(beta.as_owner(), intruder)

    status, body = _refusal(session, intruder)
    assert status == HTTPStatus.NOT_FOUND, (
        f"another firm's client {intruder.legal_name} was answered {status} rather "
        f"than 404; 403 confirms the primary key names a real client and 200 hands "
        f"it over"
    )
    assert intruder.legal_name not in body, f"{intruder.legal_name} leaked in the 404"
    assert intruder.cnpj not in body, f"{intruder.cnpj} leaked in the 404"
    assert cnpj_mask(intruder.cnpj) not in body, f"{intruder.cnpj} leaked masked"


def test_an_unassigned_client_of_the_same_firm_is_not_found_rather_than_refused(
    alpha: Firm,
) -> None:
    """The security core: 404 for a real client of your own firm, never 403.

    This is the case the cross-tenant test above cannot reach. The client is inside the
    accountant's own tenant, so every tenant filter in the stack passes it through and
    RLS has no opinion; the only thing standing between this account and the record is
    `visible_clients()`, which narrows a staff accountant to their assignments *even
    though* the published matrix grants them `clients.view_all`
    (`apps/authz/portfolio.py:55-88`).

    And the status is the whole assertion. 403 would answer "this id names a client of
    your firm and you are not on it", which is the firm's client list leaking one
    primary key at a time to an account that was deliberately not given it — a book of
    business enumerable by anyone with a session and a UUID generator. 404 answers
    nothing.

    Nothing here asserts that a role lacks `clients.view_assigned`, and re-adding such
    an assertion would pin a falsehood: that row is `(FULL, FULL, FULL, FULL, FULL,
    FULL)` at `apps/authz/matrix.py:88-92` — FULL for all six roles. The capability is
    not what confines this account. The portfolio is.

    Two gates, both inside the test. The accountant's own assigned client proves the
    screen is open to them at all, so a 404 caused by a broken route cannot pass as
    confinement; and the owner's successful render proves the unassigned record exists
    and is renderable, so the 404 cannot be passing against a missing row.
    """
    assigned, unassigned = alpha.clients[0], alpha.clients[1]

    accountant = alpha.as_accountant()
    _detail_body(accountant, assigned)
    _detail_body(alpha.as_owner(), unassigned)

    status, body = _refusal(accountant, unassigned)
    assert status == HTTPStatus.NOT_FOUND, (
        f"{TenantRole.STAFF_ACCOUNTANT} was answered {status} for "
        f"{unassigned.legal_name}, a client of their own firm they hold no assignment "
        f"on; 403 confirms the record exists and turns the URL into an enumeration "
        f"oracle for the firm's whole book of business"
    )
    assert unassigned.legal_name not in body, (
        f"{unassigned.legal_name} is named in the body of its own 404, so the screen "
        f"discloses the client it just declined to show"
    )
    assert unassigned.cnpj not in body, f"{unassigned.cnpj} leaked in the 404"
    assert cnpj_mask(unassigned.cnpj) not in body, f"{unassigned.cnpj} leaked masked"


def test_the_detail_screen_stays_inside_its_query_budget() -> None:
    """One client's page, measured against a portfolio far larger than it needs."""
    firm = make_firm("detalhe-custo", client_count=BUDGET_CLIENTS)
    measured = _detail_cost(firm, firm.clients[0])

    assert measured, "the detail request issued no queries at all"
    assert measured <= DETAIL_QUERY_BUDGET, (
        f"one client's detail page cost {measured} queries against a portfolio of "
        f"{BUDGET_CLIENTS}, over the budget of {DETAIL_QUERY_BUDGET}"
    )


def test_the_detail_query_count_does_not_grow_with_the_portfolio(alpha: Firm) -> None:
    """A page about one client must cost the same in a firm keeping sixty books.

    This is the claim that protects the product; the ceiling above is only a ratchet.
    A detail screen has no reason to touch the portfolio at all, so any dependence on
    its size is a query issued per row somewhere — and a firm large enough to matter is
    exactly the firm that discovers it.
    """
    small = _detail_cost(alpha, alpha.clients[0])
    assert small, "the small firm's detail request issued no queries at all"

    large = make_firm("detalhe-grande", client_count=BUDGET_CLIENTS)
    big = _detail_cost(large, large.clients[0])

    assert big <= small, (
        f"one client's detail page costs {big} queries in a firm of "
        f"{BUDGET_CLIENTS} clients and {small} in a firm of {SMALL_FIRM_CLIENTS}; the "
        f"page is about a single client and must not scale with the portfolio"
    )


def test_an_unregistered_municipality_renders_the_conservative_disclaimer() -> None:
    """5,543 of Brazil's municipalities are unassessed, so this IS the common path.

    The screen must say so in words. A page that renders the conservative defaults —
    no national emitter, a certificate required — without the note reports an
    unverified guess in exactly the shape of a verified fact, which is the single
    conflation `apps/fiscal/capabilities.py` exists to prevent.

    The two assertions before the render are the non-vacuity gates, and they read from
    the source rather than trusting this file's copy of it: the first fails if a future
    seed ever makes this fixture KNOWN, at which point the test would be checking the
    verified path while claiming to check the unknown one; the second fails if the
    module's wording drifts from the literal asserted against the document.
    """
    firm = make_firm("mun-ausente", client_count=1)
    company = firm.clients[0]
    _set_municipality(firm, company, ABSENT_IBGE)

    assert capability_for(ABSENT_IBGE).status == CapabilityStatus.UNKNOWN, (
        f"IBGE {ABSENT_IBGE} is in the capability registry after all, so this fixture "
        f"exercises the KNOWN path and proves nothing about the unknown one"
    )
    assert unknown_capability(ABSENT_IBGE).notes == ABSENT_DISCLAIMER, (
        f"apps/fiscal/capabilities.py now produces "
        f"{unknown_capability(ABSENT_IBGE).notes!r} rather than "
        f"{ABSENT_DISCLAIMER!r}, so the document assertion below is searching for "
        f"copy the product never emits"
    )

    body = _detail_body(firm.as_owner(), company)
    assert ABSENT_DISCLAIMER in body, (
        f"{company.legal_name} sits in an unassessed municipality and its page carries "
        f"no disclaimer, so the conservative defaults are presented as verified facts"
    )


def test_a_registered_but_unassessed_municipality_renders_its_own_disclaimer() -> None:
    """Being in the registry and having been assessed are different facts.

    The final assertion is what separates the two: a template that hardcodes one
    disclaimer, or picks it on `is_known` alone, renders the wrong sentence here and
    tells an accountant the city is unknown to the product when in truth it is known
    and merely unmeasured.
    """
    firm = make_firm("mun-sem-aval", client_count=1)
    company = firm.clients[0]
    Municipality.objects.create(ibge_code=UNASSESSED_IBGE, name="Sorocaba", uf="SP")
    _set_municipality(firm, company, UNASSESSED_IBGE)

    answer = capability_for(UNASSESSED_IBGE)
    assert answer.status == CapabilityStatus.UNKNOWN, (
        f"IBGE {UNASSESSED_IBGE} was seeded with a capability row, so this fixture no "
        f"longer exercises the registered-but-unassessed branch"
    )
    assert answer.notes == UNASSESSED_DISCLAIMER, (
        f"apps/fiscal/capabilities.py now produces {answer.notes!r} for a registered "
        f"but unassessed municipality rather than {UNASSESSED_DISCLAIMER!r}"
    )

    body = _detail_body(firm.as_owner(), company)
    assert UNASSESSED_DISCLAIMER in body, (
        f"{company.legal_name} sits in a registered municipality nobody has assessed "
        f"and its page carries no disclaimer, so conservative defaults are presented "
        f"as verified facts"
    )
    assert ABSENT_DISCLAIMER not in body, (
        f"the page reports {UNASSESSED_IBGE} as absent from the registry when the "
        f"municipality is in it and merely unassessed; the disclaimer is hardcoded "
        f"rather than read from the capability answer"
    )
