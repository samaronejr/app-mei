"""The payments list is confined by the database, and by nothing the view wrote.

This page is the first portal screen that renders *many* rows rather than one figure,
and that is what makes it the sharpest test of the isolation stack so far. A count can
be wrong by a number; a list is wrong by a whole company's fiscal calendar appearing on
somebody else's screen. The view applies **no client filter at all** — the RESTRICTIVE
policy comparing `app.client_id` is the only thing confining it — so the sibling client
below is seeded with obligations of a **different type** at **later competence months**
than anything alpha carries. Ordered `-competence_month`, an unconfined query would put
beta's rows at the very top of page one, which is a failure the eye catches rather than
one that hides in an off-by-one.

Both client roles are exercised, not just the owner. `client_owner` and
`client_collaborator` reach this page through the same login-only gate, because no
capability is FULL for both of them here — `das.generate` is VIEW_CONFIRM for the owner
and would 403 through an object-less `require_can`. RLS is the control, so RLS is what
is asserted, and it is asserted for each role separately.

The structural cases guard the other half. `obligations_obligationtype` is **not** one
of the tables `app_portal` holds SELECT on, so a `select_related("obligation_type")`
added later would not merely be slower — it would be `permission denied` on a page a
client had open. The ban is deliberately narrow: `obligation_type_id` and
`obligation_type__code` read the local column and Django trims the join away, because
the code IS that table's primary key (`apps/obligations/models/reference.py:120-125`).
Those two are permitted, and a guard that banned them would be banning the very spelling
that makes the page work.
"""

import ast
import datetime as dt
import re
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Final

import pytest
from django.db import ProgrammingError, connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.obligations.models import Obligation, ObligationStatus, ObligationType
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.support import enrol_totp

pytestmark = pytest.mark.django_db(transaction=True)

PORTAL_HOST: Final = "acme-portal.localhost"
PORTAL_URLCONF: Final = "apps.portal.urls"
PASSWORD: Final = "sufficiently-long-passphrase"  # noqa: S105

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
PORTAL_VIEWS: Final[Path] = PROJECT_ROOT / "apps" / "portal" / "views.py"
PAYMENTS_TEMPLATE: Final[Path] = (
    PROJECT_ROOT / "apps" / "portal" / "templates" / "portal" / "payments.html"
)

# The two seeded obligation types. The sibling is rendered under a type alpha never
# carries, so "the other company's rows are absent" fails on the type label as well as
# on the competence months.
DAS_CODE: Final = "DAS"
DASN_CODE: Final = "DASN"

# The pt-BR labels the TEMPLATE maps those codes onto. They may not be spelled in
# Python under `apps/` at all — `tests/obligations/test_due_rules.py` forbids the quoted
# code literal there — so the mapping lives in the page and is asserted on the render.
DAS_LABEL: Final = "Guia mensal (DAS)"
DASN_LABEL: Final = "Declaração anual (DASN)"

PAGE_SIZE: Final = 12

LAST_MONTH: Final = 12
DAS_DUE_DAY: Final = 20

# Absolute rather than today-relative on purpose: case (d) compares two rendered
# documents byte for byte, and a page carrying "vence em N dias" could not be compared
# that way at all.
ALPHA_ROWS: Final = (
    (dt.date(2027, 2, 1), ObligationStatus.SCHEDULED),
    (dt.date(2027, 1, 1), ObligationStatus.DUE),
    (dt.date(2026, 12, 1), ObligationStatus.PAID),
    (dt.date(2026, 11, 1), ObligationStatus.OVERDUE),
    (dt.date(2026, 10, 1), ObligationStatus.WAIVED),
    (dt.date(2026, 9, 1), ObligationStatus.SCHEDULED),
    (dt.date(2026, 8, 1), ObligationStatus.SCHEDULED),
    (dt.date(2026, 7, 1), ObligationStatus.SCHEDULED),
    (dt.date(2026, 6, 1), ObligationStatus.SCHEDULED),
    (dt.date(2026, 5, 1), ObligationStatus.SCHEDULED),
    (dt.date(2026, 4, 1), ObligationStatus.SCHEDULED),
    (dt.date(2026, 3, 1), ObligationStatus.SCHEDULED),
    # Beyond the first page, which is what makes `?pagina=2` a different document and
    # therefore what makes case (d)'s "every other parameter changes nothing" sharp.
    (dt.date(2026, 2, 1), ObligationStatus.SCHEDULED),
    (dt.date(2026, 1, 1), ObligationStatus.SCHEDULED),
)
ALPHA_TOTAL: Final = len(ALPHA_ROWS)

# The row whose deadline was pushed off a non-business day. Newest, so it is the first
# card on page one.
ROLLED_COMPETENCE: Final = dt.date(2027, 2, 1)
ROLLED_FROM: Final = dt.date(2027, 3, 20)
ROLLED_TO: Final = dt.date(2027, 3, 22)

ALPHA_NEWEST_COMPETENCIA: Final = "fev/2027"
# Present only on page two. Their absence from page one is what proves the paginator
# actually sliced rather than rendering the whole book.
ALPHA_PAGE_TWO_COMPETENCIAS: Final = ("fev/2026", "jan/2026")

# Later than everything alpha carries, so an unconfined `-competence_month` ordering
# would put these at the TOP of alpha's first page rather than somewhere in its tail.
BETA_ROWS: Final = (dt.date(2028, 6, 1), dt.date(2028, 5, 1))
BETA_COMPETENCIAS: Final = ("jun/2028", "mai/2028")

EMPTY_STATE: Final = (
    "Nenhuma obrigação registrada ainda. Sua contadora mantém esta lista."
)

# Every status the model defines, paired with the pt-BR word and the plain-language
# consequence the page states next to it. Read off `ObligationStatus` rather than
# invented: a status the enum grows and the page does not name would render a badge
# nobody wrote copy for.
STATUS_COPY: Final = {
    ObligationStatus.SCHEDULED: (
        "Programada",
        "Ainda não é preciso fazer nada. Avisaremos quando abrir o pagamento.",
    ),
    ObligationStatus.DUE: (
        "A pagar",
        "Pague até a data de vencimento para não pagar juros e multa.",
    ),
    ObligationStatus.PAID: (
        "Paga",
        "Nada a fazer. O pagamento já foi registrado pela sua contadora.",
    ),
    ObligationStatus.OVERDUE: (
        "Em atraso",
        "O prazo já passou. Pague o quanto antes: o valor aumenta a cada dia.",
    ),
    ObligationStatus.WAIVED: (
        "Dispensada",
        "Sua contadora dispensou esta obrigação. Você não precisa pagar.",
    ),
}

# The page's OWN cost: everything between `SET LOCAL ROLE app_portal` and the matching
# `COMMIT`. Scoped to that window rather than to the whole request for the reason
# `tests/portal/test_portal_home.py` sets out at length — roughly nineteen statements of
# middleware plumbing precede it, and a per-row query on a twelve-row page would hide
# inside a whole-request ceiling without ever breaching it.
QUERY_BUDGET: Final = 15

ROLE_SWITCH: Final = "SET LOCAL ROLE app_portal"
COMMIT: Final = "COMMIT"

TYPE_TABLE: Final = "obligations_obligationtype"
OBLIGATION_TABLE: Final = "obligations_obligation"

# Querystrings a list page might plausibly have grown, and must not have. Each is sent
# and the document compared with the unfiltered one; a page that honoured any of them
# would be an oracle a client could interrogate about rows RLS is hiding.
ORACLE_PARAMS: Final = (
    "q=DAS",
    "busca=ALPHA",
    "search=ALPHA",
    "situacao=paid",
    "status=paid",
    "tipo=DASN",
    "competencia=2028-06-01",
    "cliente=2",
)

# A join onto the type table, in every spelling that issues one.
#
#   `select_related()` bare follows EVERY foreign key, obligation_type included.
#   `select_related("obligation_type")` and its prefetch sibling say so outright.
#   `obligation_type__<field>` leaves the local column and lands on the denied table.
#
# `obligation_type_id` carries one underscore and never matches. `obligation_type__code`
# is excluded deliberately: `code` IS that table's primary key, so Django trims the join
# to the local column and no denied table is read at all.
# Everything that narrows a queryset. `Q` is here because `.filter(Q(client=…))` is the
# same restriction spelled through a different callable.
NARROWING_CALLS: Final = frozenset({"filter", "exclude", "Q"})

JOIN_ONTO_TYPE: Final = re.compile(
    r"""(?x)
      (?:select|prefetch)_related \s* \( \s* \)
    | (?:select|prefetch)_related \s* \( [^)]* obligation_type
    | obligation_type__ (?! code \b ) \w+
    """,
)


@pytest.fixture(autouse=True)
def _portal_urls(settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]
    # Pinned to the shipped tree: sibling modules in this package point the middleware
    # at `tests.portal.urls`, which mounts probes instead of pages.
    monkeypatch.setattr("apps.portal.middleware.PORTAL_URLCONF", PORTAL_URLCONF)


@dataclass(frozen=True)
class Firm:
    """One firm, two MEI clients, and three accounts that reach the portal differently.

    Both client roles are held, because the page is gated on sign-in alone and the claim
    under test is that the database confines each of them identically. The firm-side
    accountant is here to be refused.
    """

    tenant: Tenant
    alpha: ClientCompany
    beta: ClientCompany
    owner: User
    collaborator: User
    staff: User

    def signed_in_as(self, user: User) -> Client:
        """Return a test client already holding `user`'s session."""
        http = Client()
        http.force_login(user)
        return http


def _company(tenant: Tenant, legal_name: str, cnpj: str) -> ClientCompany:
    with tenant_context(tenant.id):
        # ALL_OBJECTS_OK: fixture seeding under an established tenant context.
        return ClientCompany.all_objects.create(
            tenant=tenant,
            legal_name=legal_name,
            cnpj=cnpj,
            is_mei=True,
        )


def _due_after(competence: dt.date) -> dt.date:
    """Return the twentieth of the month after `competence`, as DAS-MEI falls."""
    rolls_over = competence.month == LAST_MONTH
    year = competence.year + (1 if rolls_over else 0)
    month = 1 if rolls_over else competence.month + 1
    return dt.date(year, month, DAS_DUE_DAY)


def _obligation(
    tenant: Tenant,
    client: ClientCompany,
    obligation_type: ObligationType,
    *,
    competence: dt.date,
    status: str = ObligationStatus.SCHEDULED,
) -> None:
    nominal = _due_after(competence)
    resolved = ROLLED_TO if competence == ROLLED_COMPETENCE else nominal
    with tenant_context(tenant.id):
        Obligation.objects.create(
            tenant=tenant,
            client=client,
            obligation_type=obligation_type,
            competence_month=competence,
            nominal_due_date=ROLLED_FROM if resolved == ROLLED_TO else nominal,
            resolved_due_date=resolved,
            status=status,
        )


@pytest.fixture
def firm() -> Firm:
    tenant = Tenant.objects.create(name="Acme", slug="acme")
    alpha = _company(tenant, "CLIENTE ALPHA", "11222333000181")
    beta = _company(tenant, "CLIENTE BETA", "11444777000161")

    das = ObligationType.objects.get(code=DAS_CODE)
    dasn = ObligationType.objects.get(code=DASN_CODE)
    for competence, status in ALPHA_ROWS:
        _obligation(tenant, alpha, das, competence=competence, status=status)
    for competence in BETA_ROWS:
        _obligation(tenant, beta, dasn, competence=competence)

    owner = User.objects.create_user(email="dono@mei.example", password=PASSWORD)
    helper = User.objects.create_user(email="ajuda@mei.example", password=PASSWORD)
    staff = User.objects.create_user(email="contadora@acme.example", password=PASSWORD)
    for account in (owner, helper, staff):
        enrol_totp(account)

    Membership.objects.create(
        user=owner,
        tenant=tenant,
        role=TenantRole.CLIENT_OWNER,
        client=alpha,
    )
    Membership.objects.create(
        user=helper,
        tenant=tenant,
        role=TenantRole.CLIENT_COLLABORATOR,
        client=alpha,
    )
    # Firm-side, and therefore `client=None`: a CHECK constraint enforces the pairing.
    Membership.objects.create(user=staff, tenant=tenant, role=TenantRole.OWNER)
    return Firm(tenant, alpha, beta, owner, helper, staff)


@pytest.fixture
def bare_firm() -> Firm:
    """The same shape with alpha's book empty, so the empty state is reachable.

    Beta keeps her rows. "Alpha has nothing" and "the table has nothing" are different
    claims, and only the first of them says anything about the policy.
    """
    tenant = Tenant.objects.create(name="Acme", slug="acme")
    alpha = _company(tenant, "CLIENTE ALPHA", "11222333000181")
    beta = _company(tenant, "CLIENTE BETA", "11444777000161")

    dasn = ObligationType.objects.get(code=DASN_CODE)
    for competence in BETA_ROWS:
        _obligation(tenant, beta, dasn, competence=competence)

    owner = User.objects.create_user(email="dono@mei.example", password=PASSWORD)
    staff = User.objects.create_user(email="contadora@acme.example", password=PASSWORD)
    for account in (owner, staff):
        enrol_totp(account)
    Membership.objects.create(
        user=owner,
        tenant=tenant,
        role=TenantRole.CLIENT_OWNER,
        client=alpha,
    )
    Membership.objects.create(user=staff, tenant=tenant, role=TenantRole.OWNER)
    return Firm(tenant, alpha, beta, owner, owner, staff)


def _path() -> str:
    """Reverse the payments page against the tree the portal host actually serves.

    `reverse()` with no urlconf resolves against `ROOT_URLCONF`, which mounts none of
    the portal names, so an unqualified call raises regardless of whether this route
    exists. Reversing rather than hard-coding `/pagamentos/` also means a route
    registered a SECOND time — which shadows silently rather than erroring — is
    exercised through whichever pattern `reverse()` actually resolves to.
    """
    return str(reverse("portal-payments", urlconf=PORTAL_URLCONF))


def _payments(http: Client, query: str = "") -> str:
    """GET the payments page and return the rendered document, refusing anything else.

    The status gate is the non-vacuity control for every scan below it. A portal
    template reaching a table `app_portal` cannot read raises rather than returning a
    document, and a redirect returns an empty body that satisfies every "…is absent"
    assertion perfectly.
    """
    target = f"{_path()}?{query}" if query else _path()
    response = http.get(target, headers={"host": PORTAL_HOST})
    assert response.status_code == HTTPStatus.OK, (
        f"{target} answered {response.status_code}, so nothing scanned below is a "
        f"rendered page"
    )
    return str(response.content.decode())


def _portal_window(http: Client, query: str = "") -> list[str]:
    """Return the statements the page itself issued: the role switch to the commit.

    Mirrors `_page_statements` in `tests/portal/test_portal_home.py` — mirrored rather
    than imported, as every guard in this package is, because importing it would drag
    that module's fixtures and its own `pytestmark` in with it. The two gates below are
    the point: a slice taken from a request that never entered portal context would be
    empty, and an empty slice satisfies any ceiling at all.
    """
    target = f"{_path()}?{query}" if query else _path()
    with CaptureQueriesContext(connection) as captured:
        response = http.get(target, headers={"host": PORTAL_HOST})
    assert response.status_code == HTTPStatus.OK, response.status_code
    statements = [query_row["sql"] for query_row in captured.captured_queries]

    role_at = next(
        (index for index, sql in enumerate(statements) if ROLE_SWITCH in sql),
        None,
    )
    assert role_at is not None, (
        f"no {ROLE_SWITCH!r} was issued, so the measured window never opened and every "
        f"assertion over it holds vacuously; the statements were {statements}"
    )
    commit_at = next(
        (
            index
            for index, sql in enumerate(statements)
            if index > role_at and sql.strip() == COMMIT
        ),
        None,
    )
    assert commit_at is not None, (
        f"the portal transaction never committed: {statements}"
    )

    inside = statements[role_at + 1 : commit_at]
    assert inside, "the page issued nothing at all between the role switch and COMMIT"
    return inside


def _portal_payments_ast() -> ast.FunctionDef:
    """Return the view's parse tree, or fail rather than scan a function that moved."""
    tree = ast.parse(PORTAL_VIEWS.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "portal_payments":
            return node
    pytest.fail(f"{PORTAL_VIEWS.name} defines no portal_payments")


def _narrowing_name(call: ast.Call) -> str:
    """Name the callable, whether it is `qs.filter` or a bare `Q`."""
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return ""


def _narrows_by_client(call: ast.Call) -> bool:
    """Report whether this call restricts a queryset by client.

    `Q` counts alongside `filter` and `exclude`, because `.filter(Q(client=…))` is the
    same narrowing wearing a different hat. The prefix match catches `client`,
    `client_id` and `client__pk` in one rule.
    """
    if _narrowing_name(call) not in NARROWING_CALLS:
        return False
    return any(
        keyword.arg is not None and keyword.arg.startswith("client")
        for keyword in call.keywords
    )


# ------------------------------------------------------------- (a) the confinement


@pytest.mark.parametrize("role", ["owner", "collaborator"])
def test_the_list_is_confined_to_this_client_for_either_client_role(
    firm: Firm,
    role: str,
) -> None:
    """The page filters nothing. Both client roles still see only alpha's book.

    Beta's obligations fall at LATER competence months than any of alpha's and carry a
    different type, so an unconfined `order_by('-competence_month')` would put her rows
    at the top of this very page. That is the falsifiable half: were the RESTRICTIVE
    policy dropped, this fails on the first card rather than somewhere in a tail nobody
    reads.
    """
    body = _payments(firm.signed_in_as(getattr(firm, role)))

    assert ALPHA_NEWEST_COMPETENCIA in body, (
        f"{ALPHA_NEWEST_COMPETENCIA} is alpha's newest competence month and is missing "
        f"from her own payments page, so the absences below prove nothing"
    )
    assert DAS_LABEL in body, (
        f"{DAS_LABEL!r} is missing: the list names no obligation type, or the code "
        f"reached the page unmapped"
    )

    for competencia in BETA_COMPETENCIAS:
        assert competencia not in body, (
            f"{competencia} is the sibling client's competence month; as {role} this "
            f"page is showing another company's fiscal calendar, which is what a "
            f"dropped RESTRICTIVE policy looks like"
        )
    assert DASN_LABEL not in body, (
        f"{DASN_LABEL!r} belongs to the sibling's obligation type and alpha carries "
        f"none of that type at all"
    )
    assert firm.beta.legal_name not in body


def test_the_view_applies_no_client_filter_of_its_own() -> None:
    """The confinement above must be the database's work, not the view's.

    A `.filter(client=…)` would make the case above pass with the policy dropped, which
    is the one outcome that would leave the isolation untested while looking green.

    Read off the parse tree rather than the text, and that is not fastidiousness. The
    view's docstring explains at length why no such filter is there, and explaining it
    means SPELLING it — so a line scan reds on the prose describing the absence of the
    very thing it is looking for. `ast` sees calls, and a sentence is not a call.
    """
    view = _portal_payments_ast()

    # The gate that keeps the scan honest: were the query moved into a helper, this
    # function would no longer contain the code a filter could hide in, and the scan
    # below would pass over something that merely calls something else.
    assert any(
        isinstance(node, ast.Name) and node.id == "Obligation"
        for node in ast.walk(view)
    ), (
        "portal_payments issues no query of its own, so the scan below covers a "
        "function that could not contain the filter it is looking for"
    )

    offenders = [
        f"{PORTAL_VIEWS.name}:{call.lineno}: {_narrowing_name(call)}(...)"
        for call in ast.walk(view)
        if isinstance(call, ast.Call) and _narrows_by_client(call)
    ]
    assert offenders == [], (
        f"portal_payments narrows by client itself: {offenders}. The RESTRICTIVE "
        f"policy comparing app.client_id is the control, and a hand-written filter "
        f"would hide its removal behind a page that still looked correct"
    )


# ---------------------------------------------------------- (b) no ProgrammingError


def test_the_page_renders_without_a_denied_read_under_the_portal_role(
    firm: Firm,
) -> None:
    """Every table this page touches is one `app_portal` holds SELECT on.

    The whole render — view and template both — happens inside the transaction and
    after `SET LOCAL ROLE app_portal`, so a read of `obligations_obligationtype`,
    `tenants_tenant` or `auth_permission` surfaces as `ProgrammingError: permission
    denied`, which the test client re-raises. The role switch is asserted separately,
    because a request that fell through the middleware would raise nothing and pass
    this vacuously.
    """
    http = firm.signed_in_as(firm.owner)
    try:
        with CaptureQueriesContext(connection) as captured:
            response = http.get(_path(), headers={"host": PORTAL_HOST})
    except ProgrammingError as denied:  # pragma: no cover - the failure this guards
        pytest.fail(
            f"the payments page read a table app_portal is denied: {denied}. The fix "
            f"is always the same shape — have the view pass down the scalar it already "
            f"has, and let the template render a value rather than fetch one",
        )

    assert response.status_code == HTTPStatus.OK
    statements = [query_row["sql"] for query_row in captured.captured_queries]
    assert any(ROLE_SWITCH in sql for sql in statements), (
        f"the request never issued {ROLE_SWITCH!r}, so it never ran as the portal role "
        f"and 'no permission denied' is true of nothing; the statements were "
        f"{statements}"
    )


# --------------------------------------------------------------- (c) the no-JOIN ban


def test_the_view_never_joins_onto_the_obligation_type_table() -> None:
    """Structural, and deliberately narrow about which spellings are a join.

    `obligation_type_id` and `obligation_type__code` are join-TRIMMED local-column
    reads: the code is that table's primary key, so Django answers both from
    `obligations_obligation` alone. Banning them would ban the spelling the page is
    built on. What is banned is anything that actually reaches the denied table.
    """
    source = PORTAL_VIEWS.read_text(encoding="utf-8")
    assert "def portal_payments" in source, (
        f"{PORTAL_VIEWS.name} defines no portal_payments, so this scan quantifies over "
        f"a file that cannot contain the offence"
    )

    offenders = [
        f"{PORTAL_VIEWS.name}:{number}: {line.strip()}"
        for number, line in enumerate(source.splitlines(), start=1)
        if JOIN_ONTO_TYPE.search(line)
    ]
    assert offenders == [], (
        f"the portal view joins onto {TYPE_TABLE}: {offenders}. app_portal holds no "
        f"SELECT on that table, so this is not a slower page — it is `permission "
        f"denied` on a page a client already had open"
    )


def test_the_compiled_sql_touches_the_type_table_in_no_statement(firm: Firm) -> None:
    """The behavioural half: what the database was actually asked, not what was typed.

    A source scan pins the spellings somebody thought of. This reads the statements the
    page issued inside portal context and asserts none of them names the denied table
    at all — which holds however the join were written, and which would red on a
    template that followed the relation rather than the view.
    """
    inside = _portal_window(firm.signed_in_as(firm.owner))

    assert any(OBLIGATION_TABLE in sql for sql in inside), (
        f"no statement in the portal window touched {OBLIGATION_TABLE}, so this page "
        f"read no obligations and the absence asserted below is the absence of any "
        f"query at all; the statements were {inside}"
    )
    offenders = [sql for sql in inside if TYPE_TABLE in sql]
    assert offenders == [], (
        f"{TYPE_TABLE} appears in the compiled SQL: {offenders}. app_portal holds no "
        f"SELECT on it, so every one of these is a permission denied waiting for the "
        f"first client whose page reaches it"
    )


def test_no_portal_template_names_the_type_relation() -> None:
    """The same ban on the presentation side, where it is easiest to write by accident.

    `{{ row.obligation_type.name }}` reads like attribute access and compiles to a
    second query during rendering — inside the transaction, under the portal role. The
    view hands down a scalar precisely so the template cannot do this.
    """
    template = PAYMENTS_TEMPLATE.read_text(encoding="utf-8")
    assert template.strip(), f"{PAYMENTS_TEMPLATE} is empty"
    assert "obligation_type" not in template, (
        f"{PAYMENTS_TEMPLATE.name} names the obligation_type relation; the view passes "
        f"the raw code down as a scalar and the page renders a value"
    )


# ------------------------------------------------------------- (d) no oracle surface


def test_no_filter_or_search_parameter_changes_what_this_page_shows(
    firm: Firm,
) -> None:
    """A list a client can narrow is a list a client can interrogate.

    Filtering is how a confined page becomes an oracle: send a predicate, compare the
    result set, and learn something about the rows the policy is hiding. This page
    honours no such parameter, and the pagination case below is what stops that claim
    from being a statement about a page that never varies at all.
    """
    http = firm.signed_in_as(firm.owner)
    unfiltered = _payments(http)

    for query in ORACLE_PARAMS:
        assert _payments(http, query) == unfiltered, (
            f"?{query} changed the rendered page. A portal list that narrows on "
            f"request is an oracle: the shape of the answer reports on rows the "
            f"RESTRICTIVE policy exists to keep out of reach"
        )


def test_pagination_is_honoured_and_is_not_a_filter(firm: Firm) -> None:
    """`?pagina=` moves a window over the same confined rows; it selects nothing.

    Also the non-vacuity control for the case above. Were every querystring ignored —
    including this one — "no parameter changes the page" would be trivially true of a
    page that renders one fixed document no matter what it is asked.
    """
    http = firm.signed_in_as(firm.owner)
    first = _payments(http)
    second = _payments(http, "pagina=2")

    assert first != second, (
        "?pagina=2 rendered the same document as page one, so the paginator is not "
        "wired up — and every 'this parameter changes nothing' assertion beside it is "
        "then a statement about a page that ignores its whole querystring"
    )
    for competencia in ALPHA_PAGE_TWO_COMPETENCIAS:
        assert competencia not in first, (
            f"{competencia} belongs to page two and is on page one, so the list is not "
            f"sliced to {PAGE_SIZE} rows"
        )
        assert competencia in second, f"{competencia} is missing from page two"

    # Still this client's rows on the second page. Confinement is not a property of the
    # first slice.
    for competencia in BETA_COMPETENCIAS:
        assert competencia not in second, (
            f"{competencia} is the sibling's; paging past the first slice escaped the "
            f"confinement"
        )


# ------------------------------------------------------------- (e) the firm-side user


def test_a_firm_side_accountant_is_refused_on_the_portal_host(firm: Firm) -> None:
    """403, from the middleware, before the view is ever reached.

    `client__isnull=False` on the portal's membership lookup is the security control:
    a firm-side row carries no client, so it can never satisfy a portal request. Were
    it to, the accountant would be handed `app_portal` with `app.client_id` empty and
    the RESTRICTIVE policies would confine them to nothing — or, depending on the
    predicate, to everything.
    """
    response = firm.signed_in_as(firm.staff).get(
        _path(),
        headers={"host": PORTAL_HOST},
    )

    assert response.status_code == HTTPStatus.FORBIDDEN, (
        f"the payments page answered {response.status_code} to a firm-side membership; "
        f"the portal host serves client-role accounts only"
    )


# ----------------------------------------------------------------- what a card says


def test_every_status_carries_a_word_and_a_consequence_in_plain_portuguese(
    firm: Firm,
) -> None:
    """Never a colour or a glyph alone, and never a status word with no "so what".

    "Em atraso" tells a MEI owner where the row sits; it does not tell them the amount
    grows every day they leave it. The five statuses are read off `ObligationStatus`,
    so a sixth added later reds here rather than rendering an unlabelled badge.
    """
    body = _payments(firm.signed_in_as(firm.owner))

    assert set(STATUS_COPY) == set(ObligationStatus), (
        f"ObligationStatus defines {sorted(ObligationStatus.values)} and this case has "
        f"copy for {sorted(status.value for status in STATUS_COPY)}; a status with no "
        f"consequence line is a badge a client cannot act on"
    )
    for status, (word, consequence) in STATUS_COPY.items():
        assert word in body, f"{status.value} renders no pt-BR word: {word!r} absent"
        assert consequence in body, (
            f"{status.value} states no consequence; {consequence!r} is absent, so the "
            f"badge says where the row sits and nothing about what it costs"
        )


def test_a_due_date_is_machine_readable_and_a_rolled_one_names_its_origin(
    firm: Firm,
) -> None:
    """A `<time>` with a real datetime, and the date the deadline moved off.

    Without the note the resolved date looks like the rule's own, and a client
    reconciling against a Receita document sees two different days with no explanation.
    """
    body = _payments(firm.signed_in_as(firm.owner))

    assert f'<time datetime="{ROLLED_TO.isoformat()}">' in body, (
        "the due date is not wrapped in a <time> carrying an ISO datetime attribute"
    )
    assert ROLLED_TO.strftime("%d/%m/%Y") in body
    assert f"adiada de {ROLLED_FROM.strftime('%d/%m/%Y')}" in body, (
        "the rolled deadline does not name the date it moved from"
    )


def test_a_client_with_no_obligations_is_told_so_rather_than_shown_a_void(
    bare_firm: Firm,
) -> None:
    """An empty list and a broken page look identical without words for the difference.

    The sibling still has rows, so this also asserts the empty state is ALPHA's
    emptiness rather than the table's.
    """
    body = _payments(bare_firm.signed_in_as(bare_firm.owner))

    assert EMPTY_STATE in body
    assert DAS_LABEL not in body
    for competencia in BETA_COMPETENCIAS:
        assert competencia not in body, (
            f"{competencia} is the sibling's row on a page that just claimed to be "
            f"empty"
        )


def test_the_type_label_mapping_lives_in_the_template_and_not_in_python() -> None:
    """The codes are spelled in HTML because they may not be spelled in Python.

    `tests/obligations/test_due_rules.py` forbids the quoted literal anywhere under
    `apps/`, so an `if code == …` branch cannot creep into behavioural code and turn the
    data-driven deadline engine back into a ladder of special cases. A presentation
    label is not a branch, but the guard cannot tell the two apart — so the label
    belongs on the presentation side, where the view hands down a scalar.
    """
    template = PAYMENTS_TEMPLATE.read_text(encoding="utf-8")
    for code in (DAS_CODE, DASN_CODE):
        assert f"'{code}'" in template, (
            f"{PAYMENTS_TEMPLATE.name} does not map {code}; the label has to be chosen "
            f"here, because it cannot be chosen in Python"
        )

    source = PORTAL_VIEWS.read_text(encoding="utf-8")
    offenders = [
        f"{PORTAL_VIEWS.name}:{number}: {line.strip()}"
        for number, line in enumerate(source.splitlines(), start=1)
        for code in (DAS_CODE, DASN_CODE)
        if f'"{code}"' in line or f"'{code}'" in line
    ]
    assert offenders == [], (
        f"the portal view quotes an obligation code: {offenders}. The view passes the "
        f"raw code down as a scalar and the template decides what to call it"
    )


# ---------------------------------------------------------------------- the budget


def test_the_payments_page_stays_within_its_query_budget(firm: Firm) -> None:
    """Twelve cards, and not one of them may cost a query.

    A page that reached back into the row set once per obligation would issue twelve
    more statements than this ceiling allows. A page that renders what the view already
    fetched issues two: the count the paginator needs, and the slice.
    """
    inside = _portal_window(firm.signed_in_as(firm.owner))

    assert len(inside) <= QUERY_BUDGET, (
        f"the payments page issued {len(inside)} statements between {ROLE_SWITCH!r} "
        f"and {COMMIT}, over the budget of {QUERY_BUDGET}. That window is the page's "
        f"own work, so this is something the page grew rather than something the "
        f"middleware chain did:\n  " + "\n  ".join(inside)
    )


def test_the_first_page_shows_exactly_the_page_size(firm: Firm) -> None:
    """Twelve of fourteen, so the slice is asserted against a book that exceeds it."""
    assert ALPHA_TOTAL > PAGE_SIZE, (
        f"alpha carries {ALPHA_TOTAL} obligations and the page holds {PAGE_SIZE}; the "
        f"fixture must exceed one page or pagination is never exercised"
    )
    body = _payments(firm.signed_in_as(firm.owner))

    assert body.count(DAS_LABEL) == PAGE_SIZE, (
        f"the first page renders {body.count(DAS_LABEL)} cards rather than {PAGE_SIZE}"
    )
