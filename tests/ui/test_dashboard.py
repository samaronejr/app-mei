"""The dashboard, and with it the plan's headline exit criterion.

`test_the_exit_criterion` is the one that matters: a firm tenant with two users and
three client companies renders a correct twelve-month DAS calendar, asserted through
the HTTP layer rather than by calling the generator and believing it. The expected
dates are recomputed here from `due_date_for` — the resolver, not the stored rows — so
a calendar that renders whatever happens to be in the table cannot pass.

`test_no_trace_of_another_firm_appears` is the other half. This is the first screen a
human sees, so it is the first place the isolation model is visible, and a leak here
is a churn event rather than a bug report.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from http import HTTPStatus
from typing import Final

import pytest
from django.db.models import F
from django.urls import reverse
from pytest_django.fixtures import DjangoAssertNumQueries, SettingsWrapper

from apps.clients.models import (
    ClientCompany,
    ClientStatus,
    OnboardingItem,
    OnboardingStatus,
)
from apps.core.templatetags.ptbr import cnpj_mask
from apps.core.tenancy import tenant_context
from apps.obligations.calendar import due_date_for
from apps.obligations.generator import first_competence_for, generate_das_calendar
from apps.obligations.models import MonthlyRevenue, Obligation, ObligationType
from apps.obligations.queries import due_soon, onboarding_blocked, overdue
from tests.isolation.rolecheck import assert_isolated_role
from tests.ui.factories import Firm, add_client, assign, make_firm

pytestmark = pytest.mark.django_db(transaction=True)

TODAY = date.today()  # noqa: DTZ011
# Written out rather than imported from apps.core.dashboard. Importing the constant
# under test makes the expectation shrink with the implementation: a six-month strip
# then satisfies a twelve-month assertion, which is precisely the acceptance criterion
# this file exists to hold. Twelve is the specification, not a parameter.
EXPECTED_MONTHS = 12
USERS_PER_FIRM = 2
CLIENTS_PER_FIRM = 3
# Measured, not guessed: a dashboard render issues 79 queries, of which 53 are the
# authorization triple — membership, capability, grant — re-resolved for each of the
# seven scoped querysets the page composes. That overhead is deliberate. It could be
# removed by memoising the permission decision on the request, and it is not, because
# every read on this screen staying live is worth more than the milliseconds: this is
# the page where a stale authorization answer becomes a visible cross-tenant leak.
# What matters is that the number is O(1) in portfolio size, which the test below
# proves by comparing three clients against twenty.
QUERY_BUDGET = 90


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]


@pytest.fixture
def alpha() -> Firm:
    """The exit criterion's firm: two users, three client companies."""
    firm = make_firm("alpha-painel", client_count=CLIENTS_PER_FIRM)
    assign(firm, firm.clients[0], firm.accountant)
    with tenant_context(firm.tenant.id):
        for company in firm.clients:
            generate_das_calendar(company, starting_from=TODAY)
    return firm


def _expected_calendar(company: ClientCompany) -> list[tuple[date, date]]:
    """Recompute the twelve deadlines from the resolver, independently of the rows."""
    das = ObligationType.objects.get(code="DAS")
    first = first_competence_for(company, TODAY)
    months: list[tuple[date, date]] = []
    competence = first
    for _ in range(EXPECTED_MONTHS):
        resolved = due_date_for(das, competence)
        months.append((competence, resolved.resolved))
        competence = (competence.replace(day=28) + timedelta(days=7)).replace(day=1)
    return months


# ------------------------------------------------------------------ exit criterion


def test_the_exit_criterion(alpha: Firm) -> None:
    """Two users, three clients, a correct twelve-month DAS calendar, over HTTP."""
    assert_isolated_role()

    # Given the firm the anchor's §12 exit criterion describes
    assert alpha.tenant.memberships.count() == USERS_PER_FIRM
    with tenant_context(alpha.tenant.id):
        assert ClientCompany.objects.count() == CLIENTS_PER_FIRM

    # When its owner opens the dashboard
    response = alpha.as_owner().get(reverse("dashboard"))
    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()

    # Then the calendar strip holds twelve months for the selected client, each on
    # the date the business-day resolver independently produces for it
    with tenant_context(alpha.tenant.id):
        selected = ClientCompany.objects.order_by("legal_name", "pk").first()
        assert selected is not None
        expected = _expected_calendar(selected)

    assert len(expected) == EXPECTED_MONTHS
    for competence, resolved in expected:
        cell = (
            f'datetime="{resolved.isoformat()}"\n'
            f'                    data-competence="{competence.strftime("%Y-%m")}"'
        )
        assert cell in body, (
            f"the calendar has no cell for {competence:%Y-%m} due {resolved}"
        )


def test_the_calendar_actually_rolls_at_least_one_deadline(alpha: Firm) -> None:
    """Otherwise the assertion above would pass against a calendar that never rolls.

    Over twelve consecutive months the 20th lands on a weekend or a national holiday
    several times, so a strip with no rolled cell means the business-day calendar is
    not being consulted at all.
    """
    with tenant_context(alpha.tenant.id):
        moved = list(
            Obligation.objects.filter(obligation_type__code="DAS").exclude(
                nominal_due_date=F("resolved_due_date"),
            ),
        )
    assert moved, "no DAS deadline rolled in twelve months, which cannot be right"

    body = alpha.as_owner().get(reverse("dashboard")).content.decode()
    assert "adiado de" in body, "a rolled deadline is not shown as rolled"


# ------------------------------------------------------------------------ isolation


def test_no_trace_of_another_firm_appears(alpha: Firm) -> None:
    """Authenticated as tenant B, nothing of tenant A may reach the rendered page."""
    assert_isolated_role()
    beta = make_firm("beta-painel", client_count=2)
    with tenant_context(beta.tenant.id):
        for company in beta.clients:
            generate_das_calendar(company, starting_from=TODAY)

    body = beta.as_owner().get(reverse("dashboard")).content.decode()

    for company in alpha.clients:
        assert company.legal_name not in body
        assert company.trade_name not in body
        assert company.cnpj not in body
        assert cnpj_mask(company.cnpj) not in body
    assert alpha.tenant.name not in body
    assert alpha.owner.email not in body
    assert alpha.accountant.email not in body

    with tenant_context(alpha.tenant.id):
        for obligation in Obligation.objects.all():
            assert str(obligation.pk) not in body


def test_the_isolation_search_finds_the_identifiers_when_they_belong_there(
    alpha: Firm,
) -> None:
    """The positive control for the case above.

    "None of firm A's identifiers appear in firm B's page" is worth nothing if those
    identifiers never appear in any page — a typo in the fixture, a name the template
    does not render, and the assertion passes while testing the empty set. Every value
    searched for above is asserted here to be present in firm A's own dashboard.
    """
    body = alpha.as_owner().get(reverse("dashboard")).content.decode()
    for company in alpha.clients:
        assert company.legal_name in body
    assert alpha.tenant.name in body
    assert alpha.owner.email in body
    # The document is rendered masked, and only for the client the calendar is showing.
    # Searching a page for the stored form alone would make the isolation assertion
    # above pass against a string no template ever emits.
    with tenant_context(alpha.tenant.id):
        selected = ClientCompany.objects.order_by("legal_name", "pk").first()
    assert selected is not None
    assert cnpj_mask(selected.cnpj) in body


def test_the_portfolio_count_is_this_firms_and_not_the_platforms(alpha: Firm) -> None:
    beta = make_firm("beta-count", client_count=5)
    body = alpha.as_owner().get(reverse("dashboard")).content.decode()
    assert f"Carteira ({CLIENTS_PER_FIRM} clientes)" in body
    assert f"Carteira ({len(beta.clients)} clientes)" not in body


# -------------------------------------------------------------------------- scoping


def test_the_owner_sees_the_whole_portfolio(alpha: Firm) -> None:
    body = alpha.as_owner().get(reverse("dashboard")).content.decode()
    for company in alpha.clients:
        assert company.legal_name in body
    assert f'data-portfolio-{ClientStatus.ACTIVE}="{CLIENTS_PER_FIRM}"' in body


def test_the_staff_accountant_sees_only_their_assignments(alpha: Firm) -> None:
    body = alpha.as_accountant().get(reverse("dashboard")).content.decode()
    assert alpha.clients[0].legal_name in body
    for company in alpha.clients[1:]:
        assert company.legal_name not in body
    assert f'data-portfolio-{ClientStatus.ACTIVE}="1"' in body


def test_the_calendar_defaults_to_a_client_inside_the_portfolio(alpha: Firm) -> None:
    body = alpha.as_accountant().get(reverse("dashboard")).content.decode()
    assert alpha.clients[0].cnpj in body or alpha.clients[0].legal_name in body


def test_naming_another_firms_client_falls_back_rather_than_confirming_it(
    alpha: Firm,
) -> None:
    """A blank calendar for a real id and an error for a fake one is an oracle."""
    beta = make_firm("beta-select", client_count=1)
    response = alpha.as_owner().get(
        reverse("dashboard"),
        {"cliente": str(beta.clients[0].pk)},
    )
    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert beta.clients[0].legal_name not in body
    assert alpha.clients[0].legal_name in body


def test_an_unparseable_client_id_does_not_explode(alpha: Firm) -> None:
    response = alpha.as_owner().get(reverse("dashboard"), {"cliente": "nao-e-um-uuid"})
    assert response.status_code == HTTPStatus.OK


# --------------------------------------------------------------------------- counts


def test_the_queue_counts_match_the_queries_they_summarise(alpha: Firm) -> None:
    das = ObligationType.objects.get(code="DAS")
    with tenant_context(alpha.tenant.id):
        Obligation.objects.create(
            tenant=alpha.tenant,
            client=alpha.clients[0],
            obligation_type=das,
            competence_month=(TODAY.replace(day=1) - timedelta(days=1)).replace(day=1),
            nominal_due_date=TODAY - timedelta(days=11),
            resolved_due_date=TODAY - timedelta(days=11),
        )
        item = OnboardingItem.objects.filter(client=alpha.clients[1]).first()
        assert item is not None
        item.status = OnboardingStatus.BLOCKED
        item.blocked_reason = "aguardando procuração"
        item.save(update_fields=["status", "blocked_reason"])

        expected_due = due_soon(alpha.owner).count()
        expected_overdue = overdue(alpha.owner).count()
        expected_blocked = onboarding_blocked(alpha.owner).count()

    assert expected_overdue > 0, "the overdue count would be vacuous at zero"
    assert expected_blocked > 0, "the onboarding count would be vacuous at zero"

    body = alpha.as_owner().get(reverse("dashboard")).content.decode()
    assert f'data-count-due-soon="{expected_due}"' in body
    assert f'data-count-overdue="{expected_overdue}"' in body
    assert f'data-count-onboarding-blocked="{expected_blocked}"' in body


def test_a_threshold_client_is_listed_with_its_band(alpha: Firm) -> None:
    with tenant_context(alpha.tenant.id):
        MonthlyRevenue.objects.create(
            tenant=alpha.tenant,
            client=alpha.clients[2],
            competence_month=date(TODAY.year, 1, 1),
            gross_amount=Decimal("78000.00"),
        )
    body = alpha.as_owner().get(reverse("dashboard")).content.decode()
    assert alpha.clients[2].legal_name in body
    assert "perto do limite" in body


# ----------------------------------------------------- the way into a workspace

DETAIL_URL_NAME: Final[str] = "client-detail"
CALENDAR_STRIP_ID: Final[str] = "calendario-strip"
CALENDAR_STRIP_TEMPLATE: Final[str] = "obligations/_calendar_strip.html"


class _AnchorScanner(HTMLParser):
    """Collect every anchor with the text it labels, optionally inside one <div> id.

    The scoping matters for the calendar: this page carries a navigation bar, a skip
    link and a portfolio of tiles, all of which are anchors. Unscoped, a link drawn
    anywhere in the layout would answer for the one the strip is supposed to draw.
    """

    def __init__(self, within: str | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.entered = within is None
        self._within = within
        self._depth = 0 if within is not None else 1
        self._href: str | None = None
        self._label: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Enter the scoping element, or open an anchor inside it."""
        attributes = {key: (value or "") for key, value in attrs}
        if tag == "div" and self._within is not None:
            if self._depth:
                self._depth += 1
            elif attributes.get("id") == self._within:
                self._depth = 1
                self.entered = True
            return
        if self._depth and tag == "a":
            self._href = attributes.get("href", "")
            self._label = []

    def handle_data(self, data: str) -> None:
        """Accumulate the text of the anchor currently open, if any."""
        if self._href is not None:
            self._label.append(data)

    def handle_endtag(self, tag: str) -> None:
        """Close an anchor, or leave the scoping element."""
        if tag == "div" and self._within is not None and self._depth:
            self._depth -= 1
        elif tag == "a" and self._href is not None:
            self.links.append(("".join(self._label).strip(), self._href))
            self._href = None
            self._label = []


def _anchors_on(html: str, *, within: str | None = None, where: str) -> dict[str, str]:
    """Return `{anchor text: href}` for the links in `html`, or in one element of it.

    Both non-vacuity gates live in here rather than beside each caller, because both
    are ways for a caller to quantify over nothing and pass: markup that never
    rendered the element being scoped to, and an element that rendered no link at
    all, each yield an empty mapping — and an empty mapping satisfies every statement
    made about its entries.
    """
    scanner = _AnchorScanner(within)
    scanner.feed(html)
    scanner.close()

    assert scanner.entered, (
        f"{where}: nothing with id {within!r} rendered, so the scan never reached the "
        f"element it was scoped to and is reporting on some other markup"
    )
    assert scanner.links, (
        f"{where}: not one <a> rendered in the scanned markup, so there is no link "
        f"for the assertions below to be about"
    )
    return dict(scanner.links)


def test_the_threshold_row_links_the_client_to_their_workspace(alpha: Firm) -> None:
    """A client near the ceiling is the row acted on soonest; it has to lead there.

    The dashboard renders `obligations/_rows_threshold.html`, the same partial the
    threshold queue renders, so this covers the include as well as the partial — the
    queue passing is no evidence the dashboard's header row is wired to the same
    template.
    """
    company = alpha.clients[2]
    with tenant_context(alpha.tenant.id):
        MonthlyRevenue.objects.create(
            tenant=alpha.tenant,
            client=company,
            competence_month=date(TODAY.year, 1, 1),
            gross_amount=Decimal("78000.00"),
        )
    body = alpha.as_owner().get(reverse("dashboard")).content.decode()

    # The section has to have listed this client at all, or "it is not linked" and
    # "it is not there" are the same observation and this proves the wrong one.
    assert "perto do limite" in body
    assert company.legal_name in body, (
        "the threshold section did not list the client, so there is no row whose "
        "client could be linked"
    )

    links = _anchors_on(body, where="the dashboard")
    assert company.legal_name in links, (
        f"the dashboard names {company.legal_name!r} in its threshold table, but no "
        f"link carries that name — the ones it does carry are {sorted(links)}"
    )
    assert links[company.legal_name] == reverse(DETAIL_URL_NAME, args=[company.pk])


def test_the_calendar_names_its_client_as_a_way_into_the_workspace(
    alpha: Firm,
) -> None:
    """The strip's caption says whose calendar this is; it has to lead to them too."""
    body = alpha.as_owner().get(reverse("dashboard")).content.decode()
    links = _anchors_on(
        body,
        within=CALENDAR_STRIP_ID,
        where="the dashboard calendar strip",
    )

    expected = {
        company.legal_name: reverse(DETAIL_URL_NAME, args=[company.pk])
        for company in alpha.clients
    }
    named = {text: href for text, href in links.items() if text in expected}
    assert named, (
        f"the calendar strip carries {len(links)} link(s) — {sorted(links)} — but "
        f"none of them is labelled with a client's legal name, so the caption is not "
        f"what leads anywhere"
    )

    for legal_name, href in sorted(named.items()):
        assert href == expected[legal_name], (
            f"the calendar caption for {legal_name!r} links to {href!r}, but that "
            f"client's workspace is at {expected[legal_name]!r}"
        )


def test_the_calendar_caption_is_plain_text_unless_the_caller_asks_for_a_link(
    alpha: Firm,
) -> None:
    """The client detail screen includes this same strip, about the client it is on.

    A link there points at the page the reader is already standing on. The partial
    therefore draws a bare name by default and the dashboard opts in, so the flag
    must genuinely be off when nobody passes it — a partial that linked regardless
    would satisfy the test above and quietly put a self-link on the detail screen,
    which nothing on that screen's side asserts about.
    """
    from django.template.loader import render_to_string  # noqa: PLC0415

    company = alpha.clients[0]
    context = {"calendar_client": company, "calendar": []}

    with tenant_context(alpha.tenant.id):
        plain = render_to_string(CALENDAR_STRIP_TEMPLATE, context)
        linked = render_to_string(
            CALENDAR_STRIP_TEMPLATE,
            context
            | {
                "calendar_client_linked": True,
            },
        )

    href = reverse(DETAIL_URL_NAME, args=[company.pk])

    # Both halves, or this says nothing: without the second, a partial that never
    # links at all passes, which is the opposite failure and just as wrong.
    assert company.legal_name in plain, (
        "the unflagged render dropped the caption entirely, so its lack of a link "
        "says nothing about the flag"
    )
    assert href in linked, (
        f"the flagged render carries no link to {href}, so the flag does nothing and "
        f"the assertion below passes for the wrong reason"
    )
    assert href not in plain, (
        "the strip links its caption with no caller asking it to, which puts a link "
        "to the current page on the client detail screen"
    )


# --------------------------------------------------------------------------- shape


def test_the_dashboard_is_not_streamed(alpha: Firm) -> None:
    """A streaming response is refused by TenantMiddleware, because its iterator runs
    after the tenant transaction closes and every lazy query returns zero rows."""
    response = alpha.as_owner().get(reverse("dashboard"))
    assert not response.streaming


def test_the_dashboard_stays_inside_a_query_budget(
    alpha: Firm,
    django_assert_max_num_queries: DjangoAssertNumQueries,
) -> None:
    session = alpha.as_owner()
    with django_assert_max_num_queries(QUERY_BUDGET):
        session.get(reverse("dashboard"))


def test_the_query_count_does_not_grow_with_the_portfolio(alpha: Firm) -> None:
    """Twenty clients must cost what three cost, or the first real firm times out."""
    from django.db import connection, reset_queries  # noqa: PLC0415
    from django.test.utils import CaptureQueriesContext  # noqa: PLC0415

    from tests.ui.factories import client_base  # noqa: PLC0415

    def cost(firm: Firm) -> int:
        session = firm.as_owner()
        reset_queries()
        with CaptureQueriesContext(connection) as captured:
            session.get(reverse("dashboard"))
        return len(captured)

    small = cost(alpha)

    big = make_firm("big-painel", client_count=0)
    for index in range(20):
        add_client(
            big,
            legal_name=f"Big Cliente {index:03d} MEI",
            base=client_base(index, big.tenant.slug),
        )
    with tenant_context(big.tenant.id):
        for company in big.clients:
            generate_das_calendar(company, starting_from=TODAY)

    assert cost(big) == small


def test_the_platform_host_has_no_dashboard(alpha: Firm) -> None:
    """There is no firm there, and an empty portfolio would imply there was one."""
    from django.test import Client  # noqa: PLC0415

    session = alpha.as_owner()
    session.defaults["SERVER_NAME"] = "localhost"
    response = session.get(reverse("dashboard"))
    assert response.status_code in {HTTPStatus.NOT_FOUND, HTTPStatus.FORBIDDEN}
    assert Client(SERVER_NAME="localhost") is not None


# ------------------------------------------------- progressive enhancement (no JS)

DASHBOARD_TEMPLATE: Final[str] = "templates/core/dashboard.html"

# The tags a filter form is built from. `option` is tracked as well, because a filter
# with a single option cannot be switched at all, and asking whether such a form can
# be submitted without JavaScript would be a question about nothing.
CONTROL_TAGS: Final[frozenset[str]] = frozenset(
    {"button", "input", "option", "select", "textarea"},
)
MIN_FILTER_OPTIONS: Final[int] = 2

# The shape `templates/obligations/queue.html:33` already ships, held here as a
# literal rather than read from that file: the scanner has to be calibrated against a
# document this test owns, so that a red result on the dashboard is the dashboard's
# fault and not a parser that quietly swallows everything inside <noscript>.
KNOWN_GOOD_FILTER: Final[str] = (
    '<form method="get" hx-trigger="change">'
    '<select id="cliente" name="cliente">'
    '<option value="1">Um</option><option value="2">Dois</option>'
    "</select>"
    '<noscript><button type="submit">Filtrar</button></noscript>'
    "</form>"
)


@dataclass
class _Form:
    attrs: dict[str, str]
    controls: list[tuple[str, dict[str, str]]] = field(default_factory=list)


class _FormScanner(HTMLParser):
    """Collect each `<form>` in a rendered document together with its controls.

    `<noscript>` is deliberately not opaque here: Python's parser hides the content of
    `script` and `style` only, so a submit button written inside `<noscript>` — the
    no-JavaScript fallback the queue page already uses — arrives as a real element and
    is counted like any other. Controls outside every form are kept separately, so a
    button associated by `form="..."` can still be credited to the form it submits.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[_Form] = []
        self.detached: list[tuple[str, dict[str, str]]] = []
        self._open: list[_Form] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        pairs = {name.lower(): value or "" for name, value in attrs}
        if tag == "form":
            form = _Form(attrs=pairs)
            self.forms.append(form)
            self._open.append(form)
        elif tag in CONTROL_TAGS:
            if self._open:
                self._open[-1].controls.append((tag, pairs))
            else:
                self.detached.append((tag, pairs))

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._open:
            self._open.pop()


def _submits(tag: str, attrs: dict[str, str]) -> bool:
    kind = attrs.get("type", "").strip().lower()
    if tag == "button":
        # A <button> with no type attribute defaults to submit, per HTML.
        return kind in {"", "submit"}
    if tag == "input":
        return kind in {"submit", "image"}
    return False


def _describe(tag: str, attrs: dict[str, str]) -> str:
    return f"{tag}[type={attrs.get('type', '')}]"


def _filter_form_submits(html: str, *, source: str) -> list[str]:
    """Return the submit controls of the form owning `<select name="cliente">`.

    Both gates live in this function rather than in tests of their own on purpose.
    `pytest -k` deselects a sentinel test, and an assertion leaning on one would then
    quantify over an empty document and pass while proving nothing at all.
    """
    scanner = _FormScanner()
    scanner.feed(html)
    scanner.close()

    owning = [
        form
        for form in scanner.forms
        if any(
            tag == "select" and attrs.get("name") == "cliente"
            for tag, attrs in form.controls
        )
    ]
    assert len(owning) == 1, (
        f'{source}: expected exactly one <form> around <select name="cliente">, '
        f"found {len(owning)} — with no filter form to inspect, the submit-control "
        f"scan below would quantify over nothing and pass"
    )
    form = owning[0]

    options = [tag for tag, _ in form.controls if tag == "option"]
    assert len(options) >= MIN_FILTER_OPTIONS, (
        f"{source}: the client filter offers {len(options)} option(s), so there is "
        f"nothing to switch between and whether it can be submitted is moot"
    )

    identifier = form.attrs.get("id", "")
    associated = [
        (tag, attrs)
        for tag, attrs in scanner.detached
        if identifier and attrs.get("form") == identifier
    ]
    return [
        _describe(tag, attrs)
        for tag, attrs in [*form.controls, *associated]
        if _submits(tag, attrs)
    ]


def _calendar_filter_submits(html: str) -> list[str]:
    """Scan a dashboard document, after proving the scanner can see the target shape."""
    calibration = _filter_form_submits(
        KNOWN_GOOD_FILTER,
        source="KNOWN_GOOD_FILTER (the queue.html shape)",
    )
    assert calibration == ["button[type=submit]"], (
        f'the scanner cannot see a <noscript><button type="submit"> placed directly '
        f"in front of it (found {calibration!r}), so an empty result on the dashboard "
        f"would say nothing about the dashboard"
    )
    return _filter_form_submits(html, source=DASHBOARD_TEMPLATE)


def test_the_calendar_filter_can_be_submitted_without_javascript(alpha: Firm) -> None:
    """The calendar's client filter has to work with JavaScript switched off.

    `hx-trigger="change"` is the entire submit path today, and HTMX is JavaScript: with
    scripting unavailable — a corporate lock-down, a failed CDN fetch, a slow line where
    the document paints before the bundle arrives — the `<select>` is decoration and the
    accountant cannot look at any client but the default one. The queue page already
    ships the fix a line above its own `</form>`.
    """
    body = alpha.as_owner().get(reverse("dashboard")).content.decode()

    assert _calendar_filter_submits(body), (
        f"{DASHBOARD_TEMPLATE}: the calendar filter form around "
        f'<select name="cliente"> has no submit control — no <button type="submit">, '
        f'no <input type="submit">, '
        f"neither visible nor inside <noscript>. Without JavaScript nothing fires "
        f'hx-trigger="change", so the filter can never be applied. '
        f"templates/obligations/queue.html:33 already carries the shape this file is "
        f'missing: <noscript><button type="submit">Filtrar</button></noscript> as the '
        f"last child of the form."
    )
