"""The portal home answers two questions, and the shell survives a queued message.

A MEI owner opens this page to learn one thing: *am I late, and what do I do next*.
So the page states a situation and names a single next action, and both are asserted
here against a sibling client seeded with **different** numbers and a **different**
obligation type — the same falsifiability discipline `test_portal_login.py` uses. The
view applies no client filter at all; the RESTRICTIVE policy comparing `app.client_id`
is what confines every row it reads, and an assertion that would pass with that policy
dropped proves nothing about it.

The feedback case is a different kind of regression and is the reason this module
captures SQL rather than only scanning markup. `django_session` is **not** one of the
seven tables `app_portal` holds SELECT on. A queued message renders through
`portal/base.html`, and that render happens *inside* `PortalMiddleware`'s transaction,
after `SET LOCAL ROLE app_portal`. If anything in that window reaches the session
backend the page dies with `ProgrammingError: permission denied for table
django_session` — a 500 on the very screen that was supposed to say "enviado com
sucesso". The property that keeps it working is an **ordering**: the session is read
before the role is assumed and written after the transaction commits. Asserting only
that the words appear on the page would stay green if that ordering were lost on a
connection whose session happened to be cached, so the window between the role switch
and `COMMIT` is inspected statement by statement instead.
"""

import datetime as dt
import re
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any, Final

import pytest
from django.conf import settings
from django.contrib import messages
from django.contrib.messages.storage import default_storage
from django.db import connection
from django.http import HttpResponse
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
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
HOME_TEMPLATE: Final[Path] = (
    PROJECT_ROOT / "apps" / "portal" / "templates" / "portal" / "home.html"
)
PORTAL_VIEWS: Final[Path] = PROJECT_ROOT / "apps" / "portal" / "views.py"

# Deliberately different on each side, so every count below is falsifiable. Were the
# RESTRICTIVE policy dropped, the page would read the sum and each assertion would fail
# on its own rather than all of them agreeing with a filter the view does not apply.
ALPHA_OVERDUE: Final = 2
BETA_OVERDUE: Final = 5

# Alpha's whole book, the settled row included. The obligation count on this page has
# never been the pending count.
ALPHA_TOTAL: Final = 5

# Days from today, so the fixture never depends on the wall clock landing in a
# convenient month.
NEXT_ACTION_IN_DAYS: Final = 12
NEXT_ACTION_ROLLED_FROM_DAYS: Final = 10
LATER_ACTION_IN_DAYS: Final = 40
SIBLING_ACTION_IN_DAYS: Final = 2

# The two seeded obligation types. Named here rather than looked up so the sibling is
# rendered under a type alpha never carries — "the other company's row is absent" then
# fails on the type label as well as on the dates.
DAS_CODE: Final = "DAS"
DASN_CODE: Final = "DASN"

# The pt-BR labels the TEMPLATE maps those codes onto. They are asserted on the
# rendered page and their absence from `apps/portal/views.py` is asserted separately:
# `tests/obligations/test_due_rules.py` forbids the quoted code literal anywhere under
# `apps/`, so the mapping cannot live in Python at all.
DAS_LABEL: Final = "Guia mensal (DAS)"
DASN_LABEL: Final = "Declaração anual (DASN)"

# The competence months the two next actions cover. Distinct on purpose: `mar/2027` on
# a page that should be showing `set/2027` is a confinement failure the eye can catch.
ALPHA_NEXT_COMPETENCE: Final = dt.date(2027, 3, 1)
ALPHA_NEXT_COMPETENCIA: Final = "mar/2027"
BETA_NEXT_COMPETENCE: Final = dt.date(2027, 9, 1)
BETA_NEXT_COMPETENCIA: Final = "set/2027"

IN_GOOD_STANDING: Final = "Sua empresa está em dia"

# The page's OWN cost: everything issued between `SET LOCAL ROLE app_portal` and the
# matching `COMMIT`, which is the window the view and the template run in and the only
# part of the request this todo can move.
#
# Scoped to that window rather than to the whole request, and the difference is not
# cosmetic. A measured render costs 20 statements, of which 19 are fixed middleware
# plumbing that has nothing to do with what this page draws: the session load, the
# account, the two MFA reads, the tenant, the membership, the two GUCs, the client row,
# the three-query capability stash, the role switch and its read-back, and the access
# log. Every one of those is pinned by a test of its own elsewhere in this package, and
# counting them here would only mean the ceiling moved whenever the chain did — a
# budget that a page's own N+1 could hide inside.
#
# Fifteen against a measured four is deliberate headroom, not slack: it is roughly the
# cost of a per-obligation query over this fixture, so a card that reached back into
# the row set once per obligation lands outside it.
QUERY_BUDGET: Final = 15

MESSAGE: Final = "Documento enviado com sucesso."

SESSION_TABLE: Final = "django_session"
ROLE_SWITCH: Final = "SET LOCAL ROLE app_portal"
COMMIT: Final = "COMMIT"

SESSION_MIDDLEWARE: Final = "django.contrib.sessions.middleware.SessionMiddleware"
PORTAL_MIDDLEWARE: Final = "apps.portal.middleware.PortalMiddleware"

# `{% url 'name' %}` captured out of the template source. Read off the file rather than
# the rendered page because the failure this guards against is invisible in a render:
# `{% url 'x' as var %}` swallows a missing route and emits `href=""` on a page that
# still answers 200.
URL_TAG: Final = re.compile(r"""\{%\s*url\s+(["'])(?P<name>[\w-]+)\1""")


@pytest.fixture(autouse=True)
def _portal_urls(settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]
    # Pinned to the shipped tree: sibling modules in this package point the middleware
    # at `tests.portal.urls`, which mounts probes instead of pages.
    monkeypatch.setattr("apps.portal.middleware.PORTAL_URLCONF", PORTAL_URLCONF)


class Firm:
    """One firm, two MEI clients, and the signed-in owner of the first of them."""

    def __init__(
        self,
        tenant: Tenant,
        alpha: ClientCompany,
        beta: ClientCompany,
        owner: User,
    ) -> None:
        self.tenant = tenant
        self.alpha = alpha
        self.beta = beta
        self.owner = owner

    def as_owner(self) -> Client:
        """Return a test client signed in as alpha's owner."""
        http = Client()
        http.force_login(self.owner)
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


@dataclass(frozen=True)
class Book:
    """One client's obligations of one type, which is all either side of this seeds."""

    tenant: Tenant
    client: ClientCompany
    obligation_type: ObligationType


def _obligation(
    book: Book,
    *,
    competence: dt.date,
    nominal: dt.date,
    resolved: dt.date,
    status: str = ObligationStatus.SCHEDULED,
) -> Obligation:
    with tenant_context(book.tenant.id):
        return Obligation.objects.create(
            tenant=book.tenant,
            client=book.client,
            obligation_type=book.obligation_type,
            competence_month=competence,
            nominal_due_date=nominal,
            resolved_due_date=resolved,
            status=status,
        )


def _seed_alpha(tenant: Tenant, alpha: ClientCompany, today: dt.date) -> None:
    """Two late rows, one settled row, and two future rows the page must choose between.

    The settled row is late by date and must NOT be counted: "overdue" here means an
    unsettled obligation whose resolved date has passed, exactly as the firm-side
    queues define it. A count that trusted the date alone would tell a client who has
    already paid that they are behind.
    """
    book = Book(tenant, alpha, ObligationType.objects.get(code=DAS_CODE))
    for offset, month in ((10, 1), (3, 2)):
        late = today - dt.timedelta(days=offset)
        _obligation(
            book, competence=dt.date(2026, month, 1), nominal=late, resolved=late
        )
    settled = today - dt.timedelta(days=30)
    _obligation(
        book,
        competence=dt.date(2026, 3, 1),
        nominal=settled,
        resolved=settled,
        status=ObligationStatus.PAID,
    )
    # The next action: rolled forward off a weekend, which is what DAS-MEI does.
    _obligation(
        book,
        competence=ALPHA_NEXT_COMPETENCE,
        nominal=today + dt.timedelta(days=NEXT_ACTION_ROLLED_FROM_DAYS),
        resolved=today + dt.timedelta(days=NEXT_ACTION_IN_DAYS),
    )
    # Further out, and therefore never the next action. Without it "the earliest future
    # obligation" and "any future obligation" are the same assertion.
    later = today + dt.timedelta(days=LATER_ACTION_IN_DAYS)
    _obligation(book, competence=dt.date(2027, 4, 1), nominal=later, resolved=later)


def _seed_beta(tenant: Tenant, beta: ClientCompany, today: dt.date) -> None:
    """The sibling: more late rows than alpha, and a next action due sooner than hers.

    Sooner on purpose. Ordering by `resolved_due_date` across an unconfined table would
    put BETA's row on ALPHA's page, so the confinement case fails loudly rather than
    landing on a row that merely looks plausible.
    """
    book = Book(tenant, beta, ObligationType.objects.get(code=DASN_CODE))
    late = today - dt.timedelta(days=5)
    for month in range(1, BETA_OVERDUE + 1):
        _obligation(
            book, competence=dt.date(2026, month, 1), nominal=late, resolved=late
        )
    soon = today + dt.timedelta(days=SIBLING_ACTION_IN_DAYS)
    _obligation(book, competence=BETA_NEXT_COMPETENCE, nominal=soon, resolved=soon)


@pytest.fixture
def firm() -> Firm:
    tenant = Tenant.objects.create(name="Acme", slug="acme")
    alpha = _company(tenant, "CLIENTE ALPHA", "11222333000181")
    beta = _company(tenant, "CLIENTE BETA", "11444777000161")
    today = timezone.localdate()
    _seed_alpha(tenant, alpha, today)
    _seed_beta(tenant, beta, today)
    owner = User.objects.create_user(email="alpha@mei.example", password=PASSWORD)
    enrol_totp(owner)
    Membership.objects.create(
        user=owner,
        tenant=tenant,
        role=TenantRole.CLIENT_OWNER,
        client=alpha,
    )
    return Firm(tenant, alpha, beta, owner)


@pytest.fixture
def settled_firm(firm: Firm) -> Firm:
    """The same firm with alpha's late rows paid, so nothing of hers is outstanding.

    Written as a mutation of the seeded fixture rather than a second one, so the two
    situations differ in exactly the fact under test.
    """
    with tenant_context(firm.tenant.id):
        Obligation.objects.filter(
            client=firm.alpha,
            resolved_due_date__lt=timezone.localdate(),
        ).update(status=ObligationStatus.PAID)
    return firm


def _home(http: Client) -> str:
    """GET the portal home and return the rendered document, refusing anything else.

    The status gate is the non-vacuity control for every scan below it. A portal
    template reaching a table `app_portal` cannot read raises rather than returning a
    document, and a redirect returns an empty body that satisfies every "…is absent"
    assertion perfectly.
    """
    response = http.get("/", headers={"host": PORTAL_HOST})
    assert response.status_code == HTTPStatus.OK, (
        f"the portal home answered {response.status_code}, so nothing scanned below "
        f"is a rendered page"
    )
    return str(response.content.decode())


def _queue_success(client: Client, text: str) -> None:
    """Set a success message on `client`, through the public API.

    Mirrors `tests/ui/test_feedback.py`. `messages.success` needs a request and the
    test client hands out none, so a throwaway request is bound to the client's own
    session and cookies and the storage is flushed back onto both. Writing the
    `_messages` key by hand instead would assert against this test's idea of the wire
    format rather than Django's — and the wire format is precisely what decides
    whether the next render reaches the session backend.
    """
    request: Any = RequestFactory().get("/")
    request.session = client.session
    request.COOKIES = {name: morsel.value for name, morsel in client.cookies.items()}
    request._messages = default_storage(request)
    messages.success(request, text)

    carrier = HttpResponse()
    request._messages.update(carrier)
    request.session.save()
    for name, morsel in carrier.cookies.items():
        client.cookies[name] = morsel.value


# ------------------------------------------------------------------ (a) feedback


def test_a_queued_message_renders_without_reading_the_session_under_the_portal_role(
    firm: Firm,
) -> None:
    """The whole point is WHERE the session is read, not merely that the words appear.

    `django_session` is not one of the tables `app_portal` may SELECT, and
    `portal/base.html` draws the message queue during a render that happens inside the
    portal transaction. What keeps that from being a 500 is an ordering, so the
    ordering is what is asserted: every statement against the session table falls
    outside the window between `SET LOCAL ROLE app_portal` and the matching `COMMIT`.
    """
    http = firm.as_owner()
    _queue_success(http, MESSAGE)

    # A ProgrammingError inside the request is re-raised by the test client, so this
    # call is itself the "no permission denied" assertion — it would error here rather
    # than return a response.
    with CaptureQueriesContext(connection) as captured:
        response = http.get("/", headers={"host": PORTAL_HOST})
    statements = [query["sql"] for query in captured.captured_queries]

    assert response.status_code == HTTPStatus.OK
    assert MESSAGE in response.content.decode(), (
        "the queued confirmation never reached the rendered page, so the ordering "
        "asserted below is an ordering nobody depends on"
    )

    role_at = next(
        (index for index, sql in enumerate(statements) if ROLE_SWITCH in sql),
        None,
    )
    assert role_at is not None, (
        f"no {ROLE_SWITCH!r} was issued during the request, so the window this case "
        f"inspects does not exist and every assertion below it holds vacuously; the "
        f"statements were {statements}"
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
        f"the portal transaction never committed after {ROLE_SWITCH!r}; the "
        f"statements were {statements}"
    )

    session_reads = [
        index for index, sql in enumerate(statements) if SESSION_TABLE in sql
    ]
    assert session_reads, (
        f"the request never touched {SESSION_TABLE} at all, so 'it did not touch it "
        f"under the portal role' is true of nothing. The database session backend is "
        f"what makes this case load bearing — a cookie-only or cached backend would "
        f"pass it while proving no ordering whatsoever"
    )

    inside = [
        statements[index] for index in session_reads if role_at < index < commit_at
    ]
    assert inside == [], (
        f"{SESSION_TABLE} is queried between {ROLE_SWITCH!r} and {COMMIT}: {inside}. "
        f"app_portal holds no SELECT on that table, so this is `permission denied` on "
        f"the page a confirmation was supposed to appear on. The session must be read "
        f"before the role is assumed and written after the transaction commits"
    )


# ------------------------------------------------------------ (b) middleware order


def test_the_session_middleware_stays_outside_the_portal_transaction() -> None:
    """A position, not a preference: swapping these two 500s every portal page.

    `SessionMiddleware` outside `PortalMiddleware` is what puts both halves of the
    session's life outside the portal transaction — the load in its request phase,
    which runs before `SET LOCAL ROLE app_portal`, and the write in its response
    phase, which runs after `COMMIT` in autocommit as `app_runtime`. Reverse them and
    both land inside, where the role holds no SELECT and no write privilege on
    `django_session`: logging in, logging out and every page carrying a message become
    `permission denied`.
    """
    order = list(settings.MIDDLEWARE)
    assert SESSION_MIDDLEWARE in order, SESSION_MIDDLEWARE
    assert PORTAL_MIDDLEWARE in order, PORTAL_MIDDLEWARE

    session_at = order.index(SESSION_MIDDLEWARE)
    portal_at = order.index(PORTAL_MIDDLEWARE)
    assert session_at < portal_at, (
        f"MIDDLEWARE places {SESSION_MIDDLEWARE} at {session_at} and "
        f"{PORTAL_MIDDLEWARE} at {portal_at}. The portal one must stay INSIDE the "
        f"session one. Outside it, the session is loaded and saved within the portal "
        f"transaction, under app_portal — a role with neither SELECT nor INSERT on "
        f"django_session — so signing in, signing out and any page that carries a "
        f"message all fail with `permission denied`. This pair is load bearing at "
        f"config/settings/base.py; if a reordering is genuinely wanted, the portal "
        f"role's grants have to change first"
    )


# --------------------------------------------------------------- (c) confinement


def test_the_sibling_client_never_appears_on_the_portal_home(firm: Firm) -> None:
    """Mirrors the tracer bullet: the other company of the same firm is simply absent.

    Beta's next action falls sooner than alpha's and carries a different obligation
    type, so an unconfined `order_by('resolved_due_date').first()` would put her
    competence month and her type label on this page.
    """
    body = _home(firm.as_owner())

    assert firm.beta.legal_name not in body
    assert BETA_NEXT_COMPETENCIA not in body, (
        f"{BETA_NEXT_COMPETENCIA} is the sibling's competence month; the page is "
        f"showing another client's next obligation"
    )
    assert DASN_LABEL not in body, (
        f"{DASN_LABEL!r} belongs to the sibling's obligation type and alpha carries "
        f"none of that type at all"
    )


def test_the_situation_counts_only_this_client_s_late_obligations(firm: Firm) -> None:
    """Alpha is behind on two; the firm as a whole is behind on seven."""
    body = _home(firm.as_owner())

    assert f"Você tem {ALPHA_OVERDUE} pendências" in body
    assert f"Você tem {ALPHA_OVERDUE + BETA_OVERDUE} pendências" not in body, (
        "the situation block is reporting the firm's late obligations rather than "
        "this client's, which is what a dropped RESTRICTIVE policy looks like"
    )
    assert IN_GOOD_STANDING not in body


def test_the_obligation_count_is_still_only_this_client_s(firm: Firm) -> None:
    """The total, not the pending count, and still rendered in a bare element."""
    body = _home(firm.as_owner())

    assert f"<dd>{ALPHA_TOTAL}</dd>" in body
    assert f"<dd>{ALPHA_TOTAL + BETA_OVERDUE + 1}</dd>" not in body


# ------------------------------------------------------------------- the situation


def test_a_client_with_nothing_outstanding_is_told_so_in_words(
    settled_firm: Firm,
) -> None:
    """Never a colour or a glyph alone: the standing is stated in pt-BR."""
    body = _home(settled_firm.as_owner())

    assert IN_GOOD_STANDING in body
    assert "pendências" not in body


# ----------------------------------------------------------------- the next action


def test_the_next_action_is_the_earliest_obligation_still_ahead(firm: Firm) -> None:
    """Its type label, its competence and its due date — the later row's date absent."""
    today = timezone.localdate()
    due = today + dt.timedelta(days=NEXT_ACTION_IN_DAYS)
    later = today + dt.timedelta(days=LATER_ACTION_IN_DAYS)
    body = _home(firm.as_owner())

    assert DAS_LABEL in body, (
        f"{DAS_LABEL!r} is missing: the card names no obligation type, or the code "
        f"reached the page unmapped"
    )
    assert ALPHA_NEXT_COMPETENCIA in body
    assert due.strftime("%d/%m/%Y") in body
    assert later.strftime("%d/%m/%Y") not in body, (
        "the card is showing the obligation 40 days out rather than the earliest one "
        "still ahead"
    )


def test_the_next_action_carries_a_machine_readable_due_date(firm: Firm) -> None:
    """A `<time>` with a real `datetime`, so the date is not only ink on a screen."""
    due = timezone.localdate() + dt.timedelta(days=NEXT_ACTION_IN_DAYS)
    body = _home(firm.as_owner())

    assert f'<time datetime="{due.isoformat()}">' in body, (
        "the due date is not wrapped in a <time> carrying an ISO datetime attribute"
    )


def test_the_next_action_says_how_many_days_are_left(firm: Firm) -> None:
    """Server-computed. A template cannot subtract two dates and JS is not shipped."""
    body = _home(firm.as_owner())

    assert f"vence em {NEXT_ACTION_IN_DAYS} dias" in body, (
        "the card does not say how long is left; the count must be computed in the "
        "view, since the portal ships no JavaScript and the template language has no "
        "date arithmetic"
    )


def test_a_rolled_deadline_names_the_date_it_moved_from(firm: Firm) -> None:
    """DAS-MEI rolls FORWARD off a non-business day, and the client is told so.

    Without the note the resolved date looks like the rule's own date, and a client
    reconciling against a Receita document sees two different days with no explanation.
    """
    nominal = timezone.localdate() + dt.timedelta(days=NEXT_ACTION_ROLLED_FROM_DAYS)
    body = _home(firm.as_owner())

    assert f"adiada de {nominal.strftime('%d/%m/%Y')}" in body


def test_the_next_action_links_to_payments_and_documents() -> None:
    """Both destinations named through `{% url %}` written straight into the anchor.

    Read off the source rather than the render. `{% url 'x' as var %}` fails SILENTLY
    to `href=""` on a page that still answers 200, so a scan of a rendered document
    cannot tell a working link from a missing route — the direct form raises instead.
    """
    assert HOME_TEMPLATE.is_file(), f"{HOME_TEMPLATE} is not a shipped template"
    named = {
        match.group("name")
        for match in URL_TAG.finditer(HOME_TEMPLATE.read_text(encoding="utf-8"))
    }

    assert {"portal-payments", "portal-documents"} <= named, (
        f"{HOME_TEMPLATE.name} names {sorted(named)}; the next action has to be "
        f"actionable, which means reaching the two pages that act on it"
    )


# ------------------------------------------------------- where the labels may live


def test_the_type_label_mapping_lives_in_the_template_and_not_in_python() -> None:
    """The codes are spelled in HTML because they may not be spelled in Python.

    `tests/obligations/test_due_rules.py` forbids the quoted literal anywhere under
    `apps/`, so that an `if code == …` branch cannot creep into behavioural code and
    turn the data-driven deadline engine back into a ladder of special cases. A
    presentation label is not a branch, but the guard cannot tell the two apart — so
    the label belongs on the presentation side of the line, where the view hands down
    a scalar and the template renders a word.
    """
    template = HOME_TEMPLATE.read_text(encoding="utf-8")
    for code in (DAS_CODE, DASN_CODE):
        assert f"'{code}'" in template, (
            f"{HOME_TEMPLATE.name} does not map {code}; the label has to be chosen "
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


def _page_statements(http: Client) -> list[str]:
    """Return everything the page itself issued: the role switch to the commit.

    The same window `test_a_queued_message_renders…` inspects, measured rather than
    scanned. Bounding it from `SET LOCAL ROLE app_portal` means the count starts where
    the view's own work starts, and the gates below refuse a window that never opened —
    a slice taken from a request that fell through the middleware would be empty, and
    an empty slice satisfies any ceiling at all.
    """
    with CaptureQueriesContext(connection) as captured:
        response = http.get("/", headers={"host": PORTAL_HOST})
    assert response.status_code == HTTPStatus.OK, response.status_code
    statements = [query["sql"] for query in captured.captured_queries]

    role_at = next(
        (index for index, sql in enumerate(statements) if ROLE_SWITCH in sql),
        None,
    )
    assert role_at is not None, (
        f"no {ROLE_SWITCH!r} was issued, so the measured window never opened and any "
        f"budget would pass over an empty slice; the statements were {statements}"
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


def test_the_portal_home_stays_within_its_query_budget(firm: Firm) -> None:
    """Two cards and a definition list, and none of them may cost a query per row.

    The fixture carries six obligations for this client. A card that reached back into
    the row set once per obligation would land outside the ceiling; a card that reads
    what the view already fetched costs nothing at all.
    """
    inside = _page_statements(firm.as_owner())

    assert len(inside) <= QUERY_BUDGET, (
        f"the portal home issued {len(inside)} statements between {ROLE_SWITCH!r} and "
        f"{COMMIT}, over the budget of {QUERY_BUDGET}. That window is the page's own "
        f"work, so this is something the page grew rather than something the "
        f"middleware chain did:\n  " + "\n  ".join(inside)
    )
