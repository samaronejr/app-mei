"""The four queue screens: who may open them, what they return, and in what shape.

The authorization cases are the reason this file is written test-first. A permission
bug in a list view has a characteristic disguise — the page still returns 200 and
simply shows nothing — and "no rows" is indistinguishable from "no work to do". Every
refusal here is asserted as a 403 with a non-empty queue behind it, so a mutation that
turns the guard into a silent filter cannot pass.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from http import HTTPStatus

import pytest
from django.db.models import QuerySet
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import OnboardingItem, OnboardingStatus
from apps.core.tenancy import tenant_context
from apps.obligations.models import (
    MonthlyRevenue,
    Obligation,
    ObligationStatus,
    ObligationType,
)
from apps.obligations.views import ALL_QUEUES
from apps.tenants.models import Membership, TenantRole
from tests.ui.factories import Firm, add_member, assign, make_firm

pytestmark = pytest.mark.django_db(transaction=True)

QUEUE_URLS = (
    "queue-due-soon",
    "queue-overdue",
    "queue-onboarding",
    "queue-threshold",
)

# The two queues an operations admin is refused by the published matrix: das.generate is
# ❌ for that role, and can() answers False for it with no object in hand.
#
# queue-threshold was in this tuple and should not have been. It sat here because
# reports.view_financial is "Limited" for operations_admin and require_can passes no
# object, so the view 403'd -- which is a mechanism, not an intent. The comment above it
# described that mechanism accurately and read it as policy. The threshold queue is a
# COLLECTION view with no object for a refined level to be evaluated against, and it now
# gates on obligations.view_revenue_threshold_queue, which is FULL for this role.
REFUSED_FOR_OPERATIONS = ("queue-due-soon", "queue-overdue")

PAGE_SIZE = 25


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]


def _stock(firm: Firm, client_count: int) -> None:
    das = ObligationType.objects.get(code="DAS")
    today = date.today()  # noqa: DTZ011
    for company in firm.clients[:client_count]:
        with tenant_context(firm.tenant.id):
            Obligation.objects.create(
                tenant=firm.tenant,
                client=company,
                obligation_type=das,
                competence_month=today.replace(day=1),
                nominal_due_date=today + timedelta(days=2),
                resolved_due_date=today + timedelta(days=2),
                status=ObligationStatus.SCHEDULED,
            )
            Obligation.objects.create(
                tenant=firm.tenant,
                client=company,
                obligation_type=das,
                competence_month=(today.replace(day=1) - timedelta(days=1)).replace(
                    day=1
                ),
                nominal_due_date=today - timedelta(days=9),
                resolved_due_date=today - timedelta(days=9),
                status=ObligationStatus.SCHEDULED,
            )
            MonthlyRevenue.objects.create(
                tenant=firm.tenant,
                client=company,
                competence_month=date(today.year, 1, 1),
                gross_amount=Decimal("75000.00"),
            )
            item = OnboardingItem.objects.filter(client=company).first()
            assert item is not None
            item.status = OnboardingStatus.BLOCKED
            item.blocked_reason = "aguardando procuração"
            item.save(update_fields=["status", "blocked_reason"])


@pytest.fixture
def alpha() -> Firm:
    firm = make_firm("alpha-view", client_count=3)
    assign(firm, firm.clients[0], firm.accountant)
    _stock(firm, 3)
    return firm


def _operations_admin(firm: Firm) -> User:
    return add_member(
        firm.tenant,
        f"ops@{firm.tenant.slug}.example.com",
        TenantRole.OPERATIONS_ADMIN,
    )


# --------------------------------------------------------------------- authorization


@pytest.mark.parametrize("name", REFUSED_FOR_OPERATIONS)
def test_a_refused_queue_answers_403_and_not_an_empty_page(
    alpha: Firm,
    name: str,
) -> None:
    """The whole point. A 200 with zero rows would hide the permissions bug."""
    session = alpha.sign_in(_operations_admin(alpha))
    response = session.get(reverse(name))
    assert response.status_code == HTTPStatus.FORBIDDEN


@pytest.mark.parametrize("name", REFUSED_FOR_OPERATIONS)
def test_the_refused_queue_is_not_merely_empty_for_everyone(
    alpha: Firm,
    name: str,
) -> None:
    """Without this, the 403 above could be passing on a queue that has no rows."""
    body = alpha.as_owner().get(reverse(name)).content.decode()
    assert alpha.clients[0].legal_name in body, (
        f"{name} is empty for the owner too, so the 403 above proves nothing"
    )


@pytest.mark.parametrize("name", QUEUE_URLS)
def test_an_anonymous_visitor_is_sent_to_sign_in(alpha: Firm, name: str) -> None:
    from django.test import Client  # noqa: PLC0415

    response = Client(SERVER_NAME=alpha.host).get(reverse(name))
    assert response.status_code == HTTPStatus.FOUND
    assert reverse("account_login") in response.headers["Location"]


@pytest.mark.parametrize("name", QUEUE_URLS)
def test_every_queue_opens_for_the_owner(alpha: Firm, name: str) -> None:
    assert alpha.as_owner().get(reverse(name)).status_code == HTTPStatus.OK


# --------------------------------------------------------------------------- scoping


def test_the_owner_and_the_accountant_see_different_rows(alpha: Firm) -> None:
    owner_body = alpha.as_owner().get(reverse("queue-due-soon")).content.decode()
    staff_body = alpha.as_accountant().get(reverse("queue-due-soon")).content.decode()

    assigned = alpha.clients[0].legal_name
    unassigned = alpha.clients[1].legal_name

    assert assigned in owner_body
    assert unassigned in owner_body
    assert assigned in staff_body
    assert unassigned not in staff_body


def test_another_firms_rows_never_reach_a_queue(alpha: Firm) -> None:
    beta = make_firm("beta-view", client_count=2)
    _stock(beta, 2)
    body = alpha.as_owner().get(reverse("queue-due-soon")).content.decode()
    for company in beta.clients:
        assert company.legal_name not in body


# ---------------------------------------------------------------------------- shapes


@pytest.mark.parametrize("name", QUEUE_URLS)
def test_a_full_request_returns_a_whole_page(alpha: Firm, name: str) -> None:
    body = alpha.as_owner().get(reverse(name)).content.decode()
    assert body.lstrip().startswith("<!DOCTYPE html>")
    assert "<main" in body


@pytest.mark.parametrize("name", QUEUE_URLS)
def test_an_htmx_request_returns_only_the_fragment(alpha: Firm, name: str) -> None:
    response = alpha.as_owner().get(reverse(name), headers={"hx-request": "true"})
    body = response.content.decode()
    assert response.status_code == HTTPStatus.OK
    assert "<!DOCTYPE html>" not in body
    assert "<main" not in body
    assert 'id="fila"' in body


def test_the_fragment_carries_the_same_rows_as_the_page(alpha: Firm) -> None:
    session = alpha.as_owner()
    page = session.get(reverse("queue-due-soon")).content.decode()
    fragment = session.get(
        reverse("queue-due-soon"),
        headers={"hx-request": "true"},
    ).content.decode()
    for company in alpha.clients:
        assert (company.legal_name in page) == (company.legal_name in fragment)


def test_no_queue_response_is_streamed(alpha: Firm) -> None:
    """TenantMiddleware raises on a streaming response, because its iterator would be
    consumed after the tenant transaction closed and every lazy query would return
    zero rows — a page that downloads perfectly and is empty."""
    session = alpha.as_owner()
    for name in QUEUE_URLS:
        response = session.get(reverse(name))
        assert not response.streaming


# ------------------------------------------------------------------------ pagination


def test_pagination_is_stable_and_does_not_repeat_or_drop_rows(alpha: Firm) -> None:
    firm = make_firm("paged-view", client_count=0)
    from tests.ui.factories import add_client, client_base  # noqa: PLC0415

    for index in range(PAGE_SIZE + 7):
        add_client(
            firm,
            legal_name=f"Paged Cliente {index:03d} MEI",
            base=client_base(index, firm.tenant.slug),
        )
    _stock(firm, PAGE_SIZE + 7)

    session = firm.as_owner()

    def names_on(**params: str) -> set[str]:
        body = session.get(reverse("queue-due-soon"), params).content.decode()
        return {c.legal_name for c in firm.clients if c.legal_name in body}

    on_first = names_on()
    on_second = names_on(pagina="2")

    assert len(on_first) == PAGE_SIZE
    assert on_first & on_second == set(), "a row appears on two pages"
    assert on_first | on_second == {c.legal_name for c in firm.clients}
    # Re-requested after page two: the same page must hold the same rows, or a row
    # slips between pages and is never worked.
    assert names_on() == on_first, "the same request returned different rows"


def test_every_queue_ordering_is_total(alpha: Firm) -> None:
    """Stability is a property of the ORDER BY, not of what the planner happens to do.

    The behavioural test above cannot see this on its own: when every row shares a due
    date the comparison is fully tied, and PostgreSQL is free to return any order at
    all — it simply tends to return the same one twice in a row. Only a *total* order
    guarantees a row cannot slip between pages, and an order is total exactly when it
    ends in a unique column.
    """
    from apps.obligations.queries import (  # noqa: PLC0415
        due_soon,
        onboarding_blocked,
        overdue,
    )

    with tenant_context(alpha.tenant.id):
        for queue in (due_soon, overdue, onboarding_blocked):
            ordering = queue(alpha.owner).query.order_by
            assert ordering[-1] == "pk", (
                f"{queue.__name__} orders by {ordering}, which is not a total order"
            )


def test_an_out_of_range_page_does_not_explode(alpha: Firm) -> None:
    response = alpha.as_owner().get(reverse("queue-due-soon"), {"pagina": 999})
    assert response.status_code == HTTPStatus.OK


def test_a_nonsense_page_number_does_not_explode(alpha: Firm) -> None:
    response = alpha.as_owner().get(reverse("queue-due-soon"), {"pagina": "abc"})
    assert response.status_code == HTTPStatus.OK


# --------------------------------------------------------------------------- filters


def test_filtering_by_assignee_narrows_the_queue(alpha: Firm) -> None:
    session = alpha.as_owner()
    filtered = session.get(
        reverse("queue-due-soon"),
        {"responsavel": str(alpha.accountant.pk)},
    ).content.decode()

    assert alpha.clients[0].legal_name in filtered
    assert alpha.clients[1].legal_name not in filtered


def test_an_assignee_from_another_firm_is_ignored_rather_than_honoured(
    alpha: Firm,
) -> None:
    """Honouring it would make the filter an existence oracle for other firms' users."""
    beta = make_firm("beta-filter", client_count=1)
    response = alpha.as_owner().get(
        reverse("queue-due-soon"),
        {"responsavel": str(beta.owner.pk)},
    )
    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert alpha.clients[1].legal_name in body, "the filter silently emptied the queue"


def test_filtering_by_status_narrows_the_queue(alpha: Firm) -> None:
    with tenant_context(alpha.tenant.id):
        Obligation.objects.filter(client=alpha.clients[0]).update(
            status=ObligationStatus.DUE,
        )
    body = (
        alpha.as_owner()
        .get(
            reverse("queue-due-soon"),
            {"situacao": ObligationStatus.DUE},
        )
        .content.decode()
    )

    assert alpha.clients[0].legal_name in body
    assert alpha.clients[1].legal_name not in body


def test_every_status_accepting_queue_returns_rows_that_can_be_filtered(
    alpha: Firm,
) -> None:
    """A queue that accepts `situacao` must return something `.filter()` works on.

    This pairing used to be an invariant spanning two fields with nothing checking it:
    `accepts_status` was a constructor flag, and the threshold queue returns a tuple.
    Setting the flag on it produced `AttributeError: 'tuple' object has no attribute
    'filter'` — an HTTP 500 — the first time anybody clicked a status. The type system
    now refuses the combination; this asserts the runtime half, so reintroducing a
    settable flag and mispairing it turns this red rather than turning a page into a
    500.
    """
    # Given the queue registry, in a tenant context so the fetches see real rows
    with tenant_context(alpha.tenant.id):
        fetched = [
            (spec, spec.fetch(alpha.owner, tenant_id=alpha.tenant.id))
            for spec in ALL_QUEUES
        ]

        # Both halves must be non-empty or the guard proves nothing: with no
        # status-accepting queue it checks nothing, and with no materialised queue
        # there is no shape it could ever have caught.
        assert [spec for spec, _ in fetched if spec.accepts_status], (
            "no queue accepts a status filter — this guard would be vacuous"
        )
        assert [rows for _, rows in fetched if not isinstance(rows, QuerySet)], (
            "every queue returns a queryset, so this guard cannot see the mispairing "
            "it exists for"
        )

        # When each status-accepting queue is asked to narrow itself
        for spec, rows in fetched:
            if not spec.accepts_status:
                continue
            assert isinstance(rows, QuerySet), (
                f"{spec.slug} accepts a status filter but its fetch returned "
                f"{type(rows).__name__}, which has no .filter() — the view would 500"
            )
            narrowed = rows.filter(status=ObligationStatus.SCHEDULED)

            # Then the filter runs and actually bites
            assert 0 < narrowed.count() <= rows.count()


def test_an_unknown_status_is_ignored_rather_than_emptying_the_queue(
    alpha: Firm,
) -> None:
    body = (
        alpha.as_owner()
        .get(
            reverse("queue-due-soon"),
            {"situacao": "inexistente"},
        )
        .content.decode()
    )
    assert alpha.clients[0].legal_name in body


# ---------------------------------------------------------------------------- counts


def test_the_counts_fragment_refreshes_without_a_full_page(alpha: Firm) -> None:
    response = alpha.as_owner().get(
        reverse("queue-counts"),
        headers={"hx-request": "true"},
    )
    body = response.content.decode()
    assert response.status_code == HTTPStatus.OK
    assert "<!DOCTYPE html>" not in body
    assert 'id="contagens"' in body


def test_the_counts_match_the_queues_they_summarise(alpha: Firm) -> None:
    from apps.obligations.queries import due_soon, overdue  # noqa: PLC0415

    with tenant_context(alpha.tenant.id):
        expected_due = due_soon(alpha.owner).count()
        expected_overdue = overdue(alpha.owner).count()

    body = alpha.as_owner().get(reverse("queue-counts")).content.decode()
    assert f'data-count-due-soon="{expected_due}"' in body
    assert f'data-count-overdue="{expected_overdue}"' in body


def test_the_counts_omit_a_queue_the_account_may_not_open(alpha: Firm) -> None:
    """Reporting "14 atrasadas" to someone refused that queue leaks the number."""
    session = alpha.sign_in(_operations_admin(alpha))
    body = session.get(reverse("queue-counts")).content.decode()
    assert "data-count-overdue=" not in body
    assert "data-count-onboarding-blocked=" in body, "the fragment is not simply empty"


def test_a_membership_revoked_between_requests_stops_working(alpha: Firm) -> None:
    session = alpha.as_owner()
    assert session.get(reverse("queue-due-soon")).status_code == HTTPStatus.OK
    Membership.objects.filter(user=alpha.owner, tenant=alpha.tenant).update(
        is_active=False,
    )
    assert session.get(reverse("queue-due-soon")).status_code == HTTPStatus.FORBIDDEN


# ------------------------------------------------------- progressive enhancement

# The control the assignee filter is keyed on. Spelled out here rather than imported
# from the view, so that renaming the query parameter without renaming the form field
# — or the reverse — turns this red instead of silently agreeing with itself.
FILTER_CONTROL = "responsavel"

# A <button> carrying no type attribute submits: "submit" is the HTML default, so a
# form whose only submit control relies on that default still works with no script.
SUBMITTING_INPUT_TYPES = frozenset({"submit", "image"})
CONTROL_TAGS = frozenset({"button", "input", "select", "textarea"})
EMPTY_STATE_CLASS = "empty-state"


@dataclass(frozen=True, slots=True)
class _ScannedForm:
    """One rendered <form>: how it submits, what it carries, and whether it can."""

    method: str
    controls: tuple[str, ...]
    submit_controls: int


class _FormScanner(HTMLParser):
    """Collect every form on a page, counting what can submit each one.

    A control inside a <noscript> counts like any other, and must: it is a
    legitimate no-JavaScript fallback and the pattern the queue filter already
    ships. `html.parser` makes that countable because it treats only <script> and
    <style> as raw text, so <noscript> markup parses into real elements here — a
    scan blind to it would report a working filter as having no way to submit.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[_ScannedForm] = []
        self._open = False
        self._method = ""
        self._controls: list[str] = []
        self._submit_controls = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Open a form, or record one control inside the form already open."""
        attributes = {key: (value or "") for key, value in attrs}
        if tag == "form":
            self._open = True
            # A form with no method attribute is a GET, per HTML.
            self._method = attributes.get("method", "get").lower()
            self._controls = []
            self._submit_controls = 0
            return
        if not self._open or tag not in CONTROL_TAGS:
            return
        name = attributes.get("name", "")
        if name:
            self._controls.append(name)
        if _submits(tag, attributes):
            self._submit_controls += 1

    def handle_endtag(self, tag: str) -> None:
        """Bank the form that just closed."""
        if tag == "form" and self._open:
            self.forms.append(
                _ScannedForm(
                    method=self._method,
                    controls=tuple(self._controls),
                    submit_controls=self._submit_controls,
                ),
            )
            self._open = False


def _submits(tag: str, attributes: dict[str, str]) -> bool:
    """Report whether this element submits the form it belongs to."""
    kind = attributes.get("type", "").lower()
    if tag == "button":
        return kind in {"", "submit"}
    return tag == "input" and kind in SUBMITTING_INPUT_TYPES


def _forms_on(html: str) -> list[_ScannedForm]:
    """Parse every form out of a rendered page."""
    scanner = _FormScanner()
    scanner.feed(html)
    scanner.close()
    return scanner.forms


@pytest.mark.parametrize("name", QUEUE_URLS)
def test_the_queue_filter_can_be_applied_without_javascript(
    alpha: Firm,
    name: str,
) -> None:
    """Changing the responsible accountant must not require JavaScript.

    The select fires its request from `hx-trigger="change"`, which is a scripting
    affordance: with JavaScript off, changing the option does nothing whatsoever and
    the accountant is looking at a filter that appears to work and does not. A submit
    control — inside a <noscript> or out of one — is what turns the same form into a
    plain GET the browser performs on its own.

    A regression guard rather than a new demand: queue.html has shipped its
    <noscript> submit since the queue's first commit, and this pins that pattern so
    a later restyle of the filter cannot quietly drop it. Asserted against every
    queue because one template is rendered under four specs, and only the rendering
    can say whether all four actually receive the control.
    """
    body = alpha.as_owner().get(reverse(name)).content.decode()
    forms = _forms_on(body)

    # Non-vacuity, inside the scan rather than beside it: the page has to have parsed
    # into forms, the scan has to be telling them apart rather than matching all of
    # them, and the form it selects has to really carry the control this test is
    # named for. Without all three the loop below iterates over nothing and passes.
    assert forms, f"{name}: the rendered page parsed into no <form> at all"
    filters = [form for form in forms if FILTER_CONTROL in form.controls]
    assert filters, (
        f"{name}: no rendered form carries a {FILTER_CONTROL!r} control, so this "
        f"guard would pass over an empty list"
    )
    assert len(filters) < len(forms), (
        f"{name}: all {len(forms)} form(s) on the page matched as filter forms, so "
        f"the scan is not discriminating and the assertions below are untargeted"
    )

    for position, form in enumerate(filters, start=1):
        assert form.method == "get", (
            f"{name}: filter form #{position} in templates/obligations/queue.html "
            f"submits with method={form.method!r}; a filter that is not a GET cannot "
            f"be linked to, bookmarked, or re-run from the address bar"
        )
        assert form.submit_controls, (
            f"{name}: filter form #{position} in templates/obligations/queue.html "
            f"has no submit control — none outside <noscript> and none inside one — "
            f"so with JavaScript off, changing {FILTER_CONTROL!r} can never become a "
            f"request"
        )


# --------------------------------------------------------------- the empty state


@dataclass(frozen=True, slots=True)
class _QueueTable:
    """The queue table as rendered: its headers, its rows, and its empty cell."""

    headers: tuple[str, ...]
    body_rows: int
    empty_state_colspans: tuple[str, ...]


class _QueueTableScanner(HTMLParser):
    """Read the table inside #fila: header cells, body rows, and the empty cell.

    Scoped to #fila by counting <div> nesting from the point that id appears, so a
    table added elsewhere on the page — or in the layout — cannot be mistaken for
    the queue's own and quietly satisfy the column count below.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.headers: list[str] = []
        self.body_rows = 0
        self.empty_state_colspans: list[str] = []
        self._fila = 0
        self._in_head = False
        self._in_body = False
        self._heading: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Enter #fila, or record one cell of the table it holds."""
        attributes = {key: (value or "") for key, value in attrs}
        if tag == "div":
            self._track_fila(attributes)
        elif self._fila:
            self._record_cell(tag, attributes)

    def _track_fila(self, attributes: dict[str, str]) -> None:
        if self._fila:
            self._fila += 1
        elif attributes.get("id") == "fila":
            self._fila = 1

    def _record_cell(self, tag: str, attributes: dict[str, str]) -> None:
        if tag == "thead":
            self._in_head = True
        elif tag == "tbody":
            self._in_body = True
        elif tag == "th" and self._in_head:
            self._heading = []
        elif tag == "tr" and self._in_body:
            self.body_rows += 1
        elif tag == "td" and self._in_body:
            classes = attributes.get("class", "").split()
            if EMPTY_STATE_CLASS in classes:
                # Absent, HTML reads colspan as 1; recorded verbatim so the failure
                # can quote what the template actually shipped.
                self.empty_state_colspans.append(attributes.get("colspan", "1"))

    def handle_data(self, data: str) -> None:
        """Accumulate the text of the header cell currently open, if any."""
        if self._heading is not None:
            self._heading.append(data)

    def handle_endtag(self, tag: str) -> None:
        """Close a header cell, a section, or #fila itself."""
        if tag == "div" and self._fila:
            self._fila -= 1
            return
        if not self._fila:
            return
        if tag == "thead":
            self._in_head = False
        elif tag == "tbody":
            self._in_body = False
        elif tag == "th" and self._heading is not None:
            self.headers.append("".join(self._heading).strip())
            self._heading = None


def _queue_table_on(html: str) -> _QueueTable:
    """Parse the queue table out of a rendered page or fragment."""
    scanner = _QueueTableScanner()
    scanner.feed(html)
    scanner.close()
    return _QueueTable(
        headers=tuple(scanner.headers),
        body_rows=scanner.body_rows,
        empty_state_colspans=tuple(scanner.empty_state_colspans),
    )


@pytest.mark.parametrize("name", QUEUE_URLS)
def test_the_empty_state_spans_exactly_the_columns_the_queue_declares(
    alpha: Firm,
    name: str,
) -> None:
    """The "nothing here" cell must span the header row it sits under, per queue.

    One template renders four queues whose header counts differ — five columns for
    the two obligation queues and the threshold queue, four for onboarding — so a
    single literal in the cell cannot be right for all of them. Too few and the row
    stops short of the table's width; too many and the cell pushes the table wider
    than its own header, which is the state a screen reader reports as a malformed
    grid.

    The column count therefore has to come from `spec.headings`, the same source the
    header row is built from, rather than from a number typed once and never revisited.
    """
    from apps.obligations.views import URL_NAMES  # noqa: PLC0415

    spec = next(queue for queue in ALL_QUEUES if URL_NAMES[queue.slug] == name)

    # Somebody on this firm's roster with nothing assigned to them. Filtering by them
    # narrows every queue's book of business to zero clients, which is how the empty
    # row is reached from the outside without deleting the fixture's data.
    idle = add_member(
        alpha.tenant,
        f"vazio@{alpha.tenant.slug}.example.com",
        TenantRole.STAFF_ACCOUNTANT,
    )
    body = (
        alpha.as_owner()
        .get(reverse(name), {FILTER_CONTROL: str(idle.pk)})
        .content.decode()
    )
    table = _queue_table_on(body)
    expected = len(spec.headings)

    # Non-vacuity, inside the scan: the header row must have rendered, it must be the
    # one this spec produced, and the page must actually have reached the empty
    # branch. Any of these failing means the comparison below has nothing to compare.
    assert table.headers, (
        f"{name}: no <th> found inside #fila, so templates/obligations/_queue.html "
        f"rendered no header row and there is no column count to check against"
    )
    assert len(table.headers) == expected, (
        f"{name}: #fila rendered {len(table.headers)} header cell(s) "
        f"({', '.join(table.headers)}) but spec {spec.slug!r} declares {expected} "
        f"headings — the scan is not reading the table this spec produced"
    )
    assert table.body_rows == 1, (
        f"{name}: expected the single empty row in <tbody> but found "
        f"{table.body_rows} — filtering by an unassigned member did not empty this "
        f"queue, so the empty state was never rendered and this test proves nothing"
    )
    assert len(table.empty_state_colspans) == 1, (
        f"{name}: found {len(table.empty_state_colspans)} cell(s) carrying "
        f"class={EMPTY_STATE_CLASS!r} in templates/obligations/_queue.html, "
        f"expected exactly one"
    )

    (raw,) = table.empty_state_colspans
    assert raw.isdigit(), (
        f"{name}: the empty-state cell in templates/obligations/_queue.html carries "
        f"colspan={raw!r}, which is not a column count"
    )
    assert int(raw) == expected, (
        f"{name}: templates/obligations/_queue.html spans the empty state across "
        f"{raw} column(s) while spec {spec.slug!r} declares {expected} heading(s) "
        f"({', '.join(table.headers)}) — the colspan is hard-coded instead of coming "
        f"from spec.headings, so it is wrong for every queue whose header count is "
        f"not {raw}"
    )


# --------------------------------------------------- the way out of the queue

DETAIL_URL_NAME = "client-detail"


class _RowLinkScanner(HTMLParser):
    """Collect every anchor inside the queue table's body, with the text it labels.

    Scoped to #fila by counting <div> nesting the same way _QueueTableScanner is, so
    the layout's own navigation — which links plenty of places — cannot be mistaken
    for a queue row's link and satisfy the assertions below on its behalf.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.body_rows = 0
        self.saw_fila = False
        self._fila = 0
        self._in_body = False
        self._href: str | None = None
        self._label: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Enter #fila, or open a body row or one of its anchors."""
        attributes = {key: (value or "") for key, value in attrs}
        if tag == "div":
            if self._fila:
                self._fila += 1
            elif attributes.get("id") == "fila":
                self._fila = 1
                self.saw_fila = True
            return
        if not self._fila:
            return
        if tag == "tbody":
            self._in_body = True
        elif tag == "tr" and self._in_body:
            self.body_rows += 1
        elif tag == "a" and self._in_body:
            self._href = attributes.get("href", "")
            self._label = []

    def handle_data(self, data: str) -> None:
        """Accumulate the text of the anchor currently open, if any."""
        if self._href is not None:
            self._label.append(data)

    def handle_endtag(self, tag: str) -> None:
        """Close an anchor, the table body, or #fila itself."""
        if tag == "div" and self._fila:
            self._fila -= 1
            return
        if not self._fila:
            return
        if tag == "tbody":
            self._in_body = False
        elif tag == "a" and self._href is not None:
            self.links.append(("".join(self._label).strip(), self._href))
            self._href = None
            self._label = []


def _row_links_on(html: str, *, where: str) -> dict[str, str]:
    """Return `{anchor text: href}` for every link inside the queue table's body.

    The non-vacuity gates live in here rather than beside each caller, because every
    one of them is a way for a caller to quantify over nothing and pass: a page that
    never rendered #fila, a table with no rows in its body, or rows that carry no
    link at all each leave the mapping empty, and an empty mapping satisfies any
    statement made about its entries.
    """
    scanner = _RowLinkScanner()
    scanner.feed(html)
    scanner.close()

    assert scanner.saw_fila, (
        f"{where}: the response contains no element with id 'fila', so the scan never "
        f"entered the queue table and whatever it reports is about some other markup"
    )
    assert scanner.body_rows, (
        f"{where}: #fila rendered no <tr> inside <tbody>, so the queue is empty and "
        f"there is no row whose client could lead anywhere"
    )
    assert scanner.links, (
        f"{where}: {scanner.body_rows} queue row(s) rendered and not one of them "
        f"carries an <a> — the rows are a dead end"
    )
    return dict(scanner.links)


@pytest.mark.parametrize("name", QUEUE_URLS)
def test_every_queue_row_links_its_own_client_to_the_workspace(
    alpha: Firm,
    name: str,
) -> None:
    """A queue names a client; the name has to be the way into that client's page.

    Four queues render through four row partials, and a link added to one of them is
    no evidence about the other three — so this is asserted against every queue rather
    than against the one whose partial was edited last.

    The href is compared against the URL of the client the row is *about*, not merely
    matched for the shape of a detail URL. A partial that wrapped every name in a link
    to the same client would satisfy "there is a client-detail link on the page" and
    would send an accountant to the wrong books.
    """
    body = alpha.as_owner().get(reverse(name)).content.decode()
    links = _row_links_on(body, where=name)

    expected = {
        company.legal_name: reverse(DETAIL_URL_NAME, args=[company.pk])
        for company in alpha.clients
    }
    named = {text: href for text, href in links.items() if text in expected}

    # The fourth way this could quantify over nothing, and the one the helper cannot
    # see: the rows link somewhere, but the client name is not what was wrapped.
    assert named, (
        f"{name}: the queue rows carry {len(links)} link(s) — "
        f"{sorted(links)} — but none of them is labelled with a client's legal name, "
        f"so the client name is not what leads to the workspace"
    )

    for legal_name, href in sorted(named.items()):
        assert href == expected[legal_name], (
            f"{name}: the row for {legal_name!r} links to {href!r}, but that client's "
            f"workspace is at {expected[legal_name]!r}"
        )


@pytest.mark.parametrize("name", QUEUE_URLS)
def test_a_row_link_leads_to_a_page_this_firm_may_actually_open(
    alpha: Firm,
    name: str,
) -> None:
    """A link the product draws and then refuses is worse than no link at all."""
    session = alpha.as_owner()
    links = _row_links_on(session.get(reverse(name)).content.decode(), where=name)

    detail_urls = {
        reverse(DETAIL_URL_NAME, args=[company.pk]) for company in alpha.clients
    }
    followed = sorted(href for href in links.values() if href in detail_urls)
    assert followed, (
        f"{name}: no row link points at a client workspace, so following one proves "
        f"nothing about the queue's way out"
    )

    for href in followed:
        assert session.get(href).status_code == HTTPStatus.OK, (
            f"{name}: a queue row links to {href}, which this firm's owner is refused"
        )
