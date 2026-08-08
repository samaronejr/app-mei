"""The portal speaks consequences, and never speaks the database's own words.

Two claims, and they pull in opposite directions, which is why they are written
together.

The first is that every screen states what a situation COSTS and what to do about it,
not merely where a row sits. "Em atraso" is a label; "o valor aumenta a cada dia" is the
reason a MEI owner opens her banking app. Sibling modules already pin the badge word and
the consequence sentence for each of the five statuses; what is pinned HERE is the layer
those did not cover -- the next step at the end of each sentence, the bounded promise on
the landing page, and the two deadline rules a client most often gets wrong.

The second is that none of that vocabulary may be the stored one. `Obligation.status`
holds `scheduled`, `due`, `paid`, `overdue`, `waived` -- five English words that mean
nothing to the reader this product was built for, and which would arrive on her phone
the moment a template rendered `{{ row.status }}` or reached for the display helper
Django generates beside it. The scan at the foot of this module reads the rendered TEXT
of all four portal pages and refuses every one of those tokens.

**The two deadline rules are not symmetrical, and the copy must not pretend they are.**
DAS-MEI falls on day 20 of the following month and is postponed FORWARD to the next
business day (Resolução CGSN nº 140/2018 art. 40 §3). DASN-SIMEI falls on 31 May and
does NOT roll -- not for a Saturday, not for a Sunday, not for a holiday (art. 109). A
page that told a client her annual declaration had been "adiada" would be handing her a
few extra days the law does not give her, and that is the failure this module's annual
cases exist to catch: alpha's DASN row is seeded with DISAGREEING stored dates, so a
template that reached the roll sentence through anything other than a branch behind the
annual type would say so out loud and red here.

Fixtures are mirrored from the sibling modules in this package rather than imported, as
every guard here is: importing one would drag its fixtures and its own `pytestmark` in
with it.
"""

import datetime as dt
import html
import re
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Final

import pytest
from django.test import Client
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.templatetags.ptbr import cnpj_mask
from apps.core.tenancy import tenant_context
from apps.obligations.models import Obligation, ObligationStatus, ObligationType
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.support import enrol_totp

pytestmark = pytest.mark.django_db(transaction=True)

# `<slug>-portal.localhost`, which `portal_slug_from_host` strips back to the tenant's
# slug. Each fixture therefore carries its OWN host, because two of the cases below hold
# two fixtures at once and a slug is unique across the whole installation.
PORTAL_HOST_TEMPLATE: Final = "{slug}-portal.localhost"
PORTAL_URLCONF: Final = "apps.portal.urls"
PASSWORD: Final = "sufficiently-long-passphrase"  # noqa: S105

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
PORTAL_TEMPLATES: Final[Path] = (
    PROJECT_ROOT / "apps" / "portal" / "templates" / "portal"
)
HOME_TEMPLATE: Final[Path] = PORTAL_TEMPLATES / "home.html"

# The two seeded obligation types. Spelled here because they may not be spelled in
# Python under `apps/` at all: `tests/obligations/test_due_rules.py` forbids the
# quoted literal there, so the label mapping lives in the template and is asserted
# on a render.
DAS_CODE: Final = "DAS"
DASN_CODE: Final = "DASN"

ALPHA_CNPJ: Final = "11222333000181"

# ----------------------------------------------------------------- the copy under test
#
# Each of these is a whole sentence rather than a keyword, and that is deliberate: a
# scan for "juros" would stay green on copy that had been rewritten into something
# useless, because the alarming half of a sentence is the half that survives an edit.

# Bounded on purpose. This product can only speak for the obligations this firm's book
# carries, and a client may hold a debt her accountant has never been told about -- so
# the qualifier is the honest part of the sentence rather than a hedge around it.
BOUNDED_REASSURANCE: Final = (
    "Sua empresa está em dia com as obrigações que acompanhamos."
)

# A count with no consequence reads as a filing detail. This is the half that says a
# late obligation is not a static number.
LATE_CONSEQUENCE: Final = "o valor cresce com juros e multa"

# What the next action costs if it is missed, stated in the words of the thing owed: a
# guia is PAID and a declaration is DELIVERED, and a declaration carries a penalty even
# in a year with no tax to pay at all.
DAS_CONSEQUENCE: Final = "Pague até essa data. Depois dela há juros e multa."
DASN_CONSEQUENCE: Final = (
    "Entregue a declaração até essa data. Depois dela há multa, mesmo quando não há "
    "imposto a pagar."
)

# Resolução CGSN nº 140/2018 art. 109, recorded in docs/regulatory-watch.md: the annual
# declaration does not roll, for any reason.
DASN_FIXED_DATE: Final = (
    "A declaração anual vence em 31 de maio e essa data não muda: ela não é adiada nem "
    "quando cai em fim de semana ou feriado."
)

# Resolução CGSN nº 140/2018 art. 40 §3: DAS-MEI is postponed FORWARD. The note says why
# the two dates disagree and which of them the client is actually held to.
ROLL_HEAD: Final = "A data original caiu em fim de semana ou feriado: adiada de "
ROLL_TAIL: Final = " para o vencimento acima, que é a data que vale."

# The word an earlier pass pinned. Kept here as the token the annual cases assert the
# ABSENCE of, so "the annual row never claims it moved" is a claim about the same
# wording the monthly row uses rather than about a phrase nobody writes.
ROLL_MARKER: Final = "adiada de "

# The next step at the end of each consequence. A client told what something costs and
# not what to do about it is a client who is alarmed and stuck.
OVERDUE_NEXT_STEP: Final = (
    "Peça à sua contadora a guia atualizada, já com os juros e a multa."
)
DUE_NEXT_STEP: Final = "Se você ainda não recebeu a guia, peça à sua contadora."
SCHEDULED_NEXT_STEP: Final = "Avisaremos quando abrir o pagamento."
PAID_NEXT_STEP: Final = "Nada a fazer."
WAIVED_NEXT_STEP: Final = "Você não precisa pagar."

# Every status the model defines, paired with the thing the client can DO about it. Read
# off `ObligationStatus` rather than invented, so a sixth status added later reds here
# instead of rendering a badge with nothing actionable beside it.
STATUS_NEXT_STEP: Final = {
    ObligationStatus.SCHEDULED: SCHEDULED_NEXT_STEP,
    ObligationStatus.DUE: DUE_NEXT_STEP,
    ObligationStatus.PAID: PAID_NEXT_STEP,
    ObligationStatus.OVERDUE: OVERDUE_NEXT_STEP,
    ObligationStatus.WAIVED: WAIVED_NEXT_STEP,
}

# Dead ends, given a way out. Neither is an error, and both used to stop the reader.
NO_OPEN_OBLIGATION: Final = (
    "Nenhuma obrigação em aberto por enquanto. Avisaremos por aqui assim que a próxima "
    "abrir."
)
NO_COMPANY_LINKED: Final = (
    "Nenhuma empresa vinculada a esta conta. Fale com a sua contadora para liberar o "
    "seu acesso."
)

# The footer promise, in the words a person uses. `America/Sao_Paulo` is an IANA
# database key: correct, and meaningless on a MEI owner's phone.
TIMEZONE_PROMISE: Final = "Todos os horários desta página são de Brasília."
IANA_KEY: Final = "America/Sao_Paulo"

# ------------------------------------------------------------- the enum-leak scan
#
# SHAPE: tags and attributes are stripped FIRST, and only then is each token matched
# with word boundaries against what is left. Both halves are load-bearing, and neither
# is sufficient alone.
#
#   Boundaries alone are not enough. Four of the five stored values are ordinary English
#   words that live perfectly legitimately inside markup -- `<time datetime="…">` on
#   every card, a `badge-…` class name, an `aria-…` attribute, a URL segment. A hyphen
#   and a quote are both word boundaries, so `\bpaid\b` matches a `badge-paid` class as
#   readily as it matches the word on the screen, and a scan that reds on the markup is
#   a scan somebody will "fix" by weakening it until it means nothing.
#
#   Stripping alone is not enough either. Once the tags are gone the remainder is pt-BR
#   prose, and a substring test over prose is a trap waiting for the first sentence that
#   happens to contain one of these letter runs inside a longer word.
#
# So: strip to text nodes, then match whole words in what a person can actually read.
# `_visible_text` carries the non-vacuity gate, because a scan of an empty string --
# a redirect, a 403, a template that rendered nothing -- passes every absence assertion
# ever written.

SCRIPT_OR_STYLE: Final = re.compile(
    r"<(script|style)\b.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
HTML_COMMENT: Final = re.compile(r"<!--.*?-->", re.DOTALL)
TAG: Final = re.compile(r"<[^>]*>")
RUN_OF_SPACE: Final = re.compile(r"\s+")

# The stored values AND the labels Django generates beside them. `get_status_display()`
# returns the label, so a template reaching for it leaks a different string from the one
# `{{ row.status }}` would -- today they happen to agree, and a translation of the model
# choices would make them differ without either becoming acceptable to show a client.
STATUS_TOKENS: Final = frozenset(
    {str(status.value) for status in ObligationStatus}
    | {str(status.label) for status in ObligationStatus},
)

# The display helper itself, banned structurally as well as behaviourally. The scan
# above only sees the branches a fixture reaches; this one covers every branch there is.
# Written as a fragment rather than the whole call so `{{ row.get_status_display }}` and
# `{{ row.get_status_display|title }}` are both caught.
DISPLAY_HELPER: Final = "get_status_display"

EXPECTED_TEMPLATES: Final = frozenset(
    {
        "base.html",
        "home.html",
        "payments.html",
        "documents.html",
        "account.html",
        "upload_error.html",
        "partials/nav.html",
        "partials/upload_form.html",
    },
)

# ------------------------------------------------------------------------ the fixture

# Today-relative, so no case depends on the wall clock landing in a convenient month.
NEXT_ACTION_ROLLED_TO: Final = 12
NEXT_ACTION_ROLLED_FROM: Final = 10
LATER_DAS: Final = 40
FIRST_OVERDUE: Final = -30
SECOND_OVERDUE: Final = -45
PAID_DAYS: Final = -60
WAIVED_DAYS: Final = -90
ANNUAL_DAYS: Final = 200

ALPHA_OVERDUE: Final = 2

# The annual row's two stored dates DISAGREE, which the deadline engine would never
# produce and which is exactly the point: it is the only seed under which "the annual
# row never claims its deadline moved" is a falsifiable statement rather than a remark
# about a branch no fixture reaches.
ANNUAL_NOMINAL_OFFSET: Final = 198

# The annual row is the earliest thing ahead here, and the monthly one is deliberately
# later, so "the card is the annual obligation" fails rather than passing by default.
ANNUAL_FIRST: Final = 30
ANNUAL_FIRST_NOMINAL: Final = 28
ANNUAL_FIRST_LATER_DAS: Final = 90

MONTH: Final = 31


@pytest.fixture(autouse=True)
def _portal_urls(settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]
    monkeypatch.setattr("apps.portal.middleware.PORTAL_URLCONF", PORTAL_URLCONF)


@dataclass(frozen=True)
class Firm:
    """One firm, one MEI client, and the account that reads the portal as her."""

    tenant: Tenant
    alpha: ClientCompany
    owner: User

    @property
    def host(self) -> str:
        """Return the portal hostname this firm's slug is reachable at."""
        return PORTAL_HOST_TEMPLATE.format(slug=self.tenant.slug)

    def signed_in(self) -> Client:
        """Return a test client already holding the owner's session."""
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


def _competence(index: int) -> dt.date:
    """Return a distinct first-of-month, so no two seeded rows collide on a period."""
    today = dt.date.today()  # noqa: DTZ011 - a competence label, never a deadline
    total = today.year * 12 + (today.month - 1) + index
    return dt.date(total // 12, total % 12 + 1, 1)


@dataclass(frozen=True)
class Book:
    """One client's obligations, seeded a row at a time in days from today.

    Written as a bound method rather than a free function so the tenant and the client
    travel together: every row in this module belongs to the same company, and passing
    the pair separately at each call site is three chances per row to seed an obligation
    under a company the signed-in account cannot see — which reads, on the page, exactly
    like copy that failed to render.
    """

    tenant: Tenant
    client: ClientCompany

    def add(
        self,
        obligation_type: ObligationType,
        *,
        index: int,
        resolved_in: int,
        nominal_in: int | None = None,
        status: str = ObligationStatus.SCHEDULED,
    ) -> None:
        """Seed one obligation `resolved_in` days out, on its own competence month."""
        today = dt.date.today()  # noqa: DTZ011 - matched to the view's `localdate()`
        offset = resolved_in if nominal_in is None else nominal_in
        with tenant_context(self.tenant.id):
            Obligation.objects.create(
                tenant=self.tenant,
                client=self.client,
                obligation_type=obligation_type,
                competence_month=_competence(index),
                nominal_due_date=today + dt.timedelta(days=offset),
                resolved_due_date=today + dt.timedelta(days=resolved_in),
                status=status,
            )


def _seed(slug: str) -> tuple[Tenant, ClientCompany, User]:
    """Return one firm, its single MEI client, and the account that reads as her.

    The slug distinguishes each fixture's tenant, its portal hostname and its account,
    because two of the cases below hold two fixtures at once and a tenant slug, a
    hostname and an account address are each unique across the whole installation rather
    than within one firm. The CNPJ is NOT varied: its uniqueness is scoped to the
    tenant, so every fixture seeds the same company and the mask assertion names one
    string.
    """
    tenant = Tenant.objects.create(name=f"Acme {slug}", slug=slug)
    alpha = _company(tenant, "CLIENTE ALPHA", ALPHA_CNPJ)
    owner = User.objects.create_user(
        email=f"dona-{slug}@mei.example",
        password=PASSWORD,
    )
    enrol_totp(owner)
    Membership.objects.create(
        user=owner,
        tenant=tenant,
        role=TenantRole.CLIENT_OWNER,
        client=alpha,
    )
    return tenant, alpha, owner


@pytest.fixture
def firm() -> Firm:
    """Every status on one page, one rolled monthly guia, and one annual declaration."""
    tenant, alpha, owner = _seed("todas")
    book = Book(tenant, alpha)
    das = ObligationType.objects.get(code=DAS_CODE)
    dasn = ObligationType.objects.get(code=DASN_CODE)

    # The next action: unsettled, still ahead, and postponed off a non-business day.
    book.add(
        das,
        index=1,
        resolved_in=NEXT_ACTION_ROLLED_TO,
        nominal_in=NEXT_ACTION_ROLLED_FROM,
        status=ObligationStatus.DUE,
    )
    book.add(das, index=2, resolved_in=LATER_DAS)
    for offset, index in ((FIRST_OVERDUE, -1), (SECOND_OVERDUE, -2)):
        book.add(
            das,
            index=index,
            resolved_in=offset,
            status=ObligationStatus.OVERDUE,
        )
    book.add(das, index=-3, resolved_in=PAID_DAYS, status=ObligationStatus.PAID)
    book.add(das, index=-4, resolved_in=WAIVED_DAYS, status=ObligationStatus.WAIVED)
    # Disagreeing dates on purpose. See ANNUAL_NOMINAL_OFFSET above.
    book.add(
        dasn,
        index=MONTH,
        resolved_in=ANNUAL_DAYS,
        nominal_in=ANNUAL_NOMINAL_OFFSET,
    )
    return Firm(tenant, alpha, owner)


@pytest.fixture
def annual_firm() -> Firm:
    """The annual declaration is the next action, and it too has disagreeing dates."""
    tenant, alpha, owner = _seed("anual")
    book = Book(tenant, alpha)

    book.add(
        ObligationType.objects.get(code=DASN_CODE),
        index=1,
        resolved_in=ANNUAL_FIRST,
        nominal_in=ANNUAL_FIRST_NOMINAL,
    )
    book.add(
        ObligationType.objects.get(code=DAS_CODE),
        index=2,
        resolved_in=ANNUAL_FIRST_LATER_DAS,
    )
    return Firm(tenant, alpha, owner)


@pytest.fixture
def settled_firm() -> Firm:
    """Nothing late, so the bounded reassurance is the branch that renders."""
    tenant, alpha, owner = _seed("quitada")
    book = Book(tenant, alpha)
    das = ObligationType.objects.get(code=DAS_CODE)

    book.add(das, index=-1, resolved_in=PAID_DAYS, status=ObligationStatus.PAID)
    book.add(das, index=1, resolved_in=LATER_DAS)
    return Firm(tenant, alpha, owner)


@pytest.fixture
def unlinked_firm() -> Firm:
    """An account whose client carries no obligations at all.

    Alpha exists and is linked, because the portal host refuses a membership without a
    client before a view is ever entered; what this fixture leaves empty is her BOOK,
    which is the state the landing page's empty branch is written for.
    """
    return Firm(*_seed("vazia"))


# --------------------------------------------------------------------------- helpers


def _fetch(firm: Firm, name: str) -> str:
    """GET one portal page and return the rendered document, refusing anything else.

    The status gate is the non-vacuity control for every scan below it: a redirect or a
    403 returns a body that satisfies each "…is absent" assertion perfectly. It also
    catches the host being wrong, which is the quietest failure available here — an
    unresolvable slug leaves `request.client` unset, and the landing page then answers
    200 with its whole body replaced by the no-company branch.
    """
    target = str(reverse(name, urlconf=PORTAL_URLCONF))
    response = firm.signed_in().get(target, headers={"host": firm.host})
    assert response.status_code == HTTPStatus.OK, (
        f"{target} answered {response.status_code}, so nothing asserted about it is a "
        f"statement about a rendered page"
    )
    return str(response.content.decode())


def _visible_text(document: str, *, sentinel: str) -> str:
    """Return only what a reader sees: tags, attributes and entities all resolved away.

    The two gates are what stop this from being a scan of nothing. A template that
    rendered an empty document, or one whose real content sat somewhere this stripper
    threw away, would leave a string in which no banned token appears — and every
    absence asserted over it would hold for the wrong reason.
    """
    without_code = HTML_COMMENT.sub(" ", SCRIPT_OR_STYLE.sub(" ", document))
    text = RUN_OF_SPACE.sub(" ", html.unescape(TAG.sub(" ", without_code))).strip()

    assert text, (
        "stripping the markup left no text at all, so every token this scan refuses is "
        "absent from an empty string"
    )
    assert sentinel in text, (
        f"{sentinel!r} is missing from the stripped text, so the stripper threw the "
        f"page's own content away and every absence below is an absence from the "
        f"wrong string"
    )
    return text


def _bare_status_tokens(text: str) -> list[str]:
    """Return every stored status word appearing as a WORD in what a reader sees."""
    return sorted(
        token
        for token in STATUS_TOKENS
        if re.search(rf"\b{re.escape(token)}\b", text, re.IGNORECASE)
    )


def _portal_templates() -> list[Path]:
    found = sorted(PORTAL_TEMPLATES.rglob("*.html"))
    relative = {path.relative_to(PORTAL_TEMPLATES).as_posix() for path in found}
    assert relative == EXPECTED_TEMPLATES, (
        f"the portal ships {sorted(relative)} and this module expects "
        f"{sorted(EXPECTED_TEMPLATES)}; a template renamed or added silently shrinks "
        f"or widens every scan quantified over this set"
    )
    return found


# ------------------------------------------------------- the landing page's promises


def test_the_reassurance_names_what_this_product_actually_watches(
    settled_firm: Firm,
) -> None:
    """An unqualified "em dia" is a guarantee this product cannot make.

    A MEI owner can hold a debt her accountant has never been told about. The qualifier
    scopes the claim to the book this firm keeps, which is the only thing the page read.
    """
    body = _fetch(settled_firm, "portal-home")

    assert BOUNDED_REASSURANCE in body, (
        f"{BOUNDED_REASSURANCE!r} is absent; an unqualified 'em dia' promises a client "
        f"her whole fiscal life is clear, which is a claim about records this page "
        f"never read"
    )


def test_a_late_count_says_what_it_costs_to_leave_it(firm: Firm) -> None:
    """A number is a filing detail. The sentence beside it is the reason to act."""
    body = _fetch(firm, "portal-home")

    assert f"Você tem {ALPHA_OVERDUE} pendências" in body, (
        "the late count is missing, so the consequence asserted below would be a "
        "sentence attached to nothing"
    )
    assert LATE_CONSEQUENCE in body, (
        f"{LATE_CONSEQUENCE!r} is absent: the page reports how many obligations are "
        f"late and never says the figure grows while they stay that way"
    )
    assert BOUNDED_REASSURANCE not in body, (
        "a client with late obligations is being reassured she is in good standing"
    )


def test_the_next_action_says_what_missing_the_date_costs(firm: Firm) -> None:
    """The monthly guia is PAID, and the penalty for missing it is juros e multa."""
    body = _fetch(firm, "portal-home")

    assert DAS_CONSEQUENCE in body, (
        f"{DAS_CONSEQUENCE!r} is absent; the card states a date and never says what "
        f"happens on the far side of it"
    )


def test_both_landing_dead_ends_offer_a_way_out(
    unlinked_firm: Firm,
    settled_firm: Firm,
) -> None:
    """An empty screen that stops the reader is a support ticket waiting to happen.

    The two branches are asserted together because they are the same mistake at two
    depths: nothing to show, and nothing to do about it.
    """
    empty_book = _fetch(unlinked_firm, "portal-home")
    assert NO_OPEN_OBLIGATION in empty_book, (
        f"{NO_OPEN_OBLIGATION!r} is absent: a client with an empty book is shown a "
        f"void, and told neither that it is expected nor that she will hear from us"
    )

    # The company-linked branch is unreachable through the portal host, which refuses a
    # membership carrying no client before a view is ever entered — so it is asserted on
    # the source, where it is the only place it can be asserted at all.
    source = HOME_TEMPLATE.read_text(encoding="utf-8")
    assert NO_COMPANY_LINKED in source, (
        f"{NO_COMPANY_LINKED!r} is absent from {HOME_TEMPLATE.name}; the no-company "
        f"branch names the problem and offers nothing to do about it"
    )

    # And the reassurance branch is genuinely a different document, so the case above is
    # not quietly asserting things about one page rendered twice.
    assert BOUNDED_REASSURANCE in _fetch(settled_firm, "portal-home")


# ---------------------------------------------------------------- the two roll rules


def test_a_postponed_monthly_guia_explains_why_and_which_date_counts(
    firm: Firm,
) -> None:
    """DAS-MEI is postponed FORWARD, and two dates with no explanation is a trap.

    Sibling modules pin that the origin date appears. What is pinned here is the rest of
    the sentence: the reason the two disagree, and which of them the client is held to.
    """
    origin = (
        dt.date.today()  # noqa: DTZ011 - mirrors the fixture's own offset arithmetic
        + dt.timedelta(days=NEXT_ACTION_ROLLED_FROM)
    ).strftime("%d/%m/%Y")
    body = _fetch(firm, "portal-home")

    assert f"{ROLL_HEAD}{origin}{ROLL_TAIL}" in body, (
        f"the postponed deadline does not explain itself; expected "
        f"{ROLL_HEAD + origin + ROLL_TAIL!r}"
    )


@pytest.mark.parametrize("page", ["portal-home", "portal-payments"])
def test_the_annual_declaration_is_never_described_as_postponed(
    annual_firm: Firm,
    page: str,
) -> None:
    """DASN-SIMEI falls on 31 May and does NOT roll, for any reason at all.

    Resolução CGSN nº 140/2018 art. 109. The fixture seeds this row with DISAGREEING
    stored dates precisely so this is falsifiable: a template that reached the roll
    sentence on `rolled` before checking the type would announce a postponement the law
    does not grant, and the client would read it as a few extra days she does not have.
    """
    body = _fetch(annual_firm, page)

    assert DASN_FIXED_DATE in body, (
        f"{page} does not state the annual declaration's fixed date; "
        f"{DASN_FIXED_DATE!r} is absent"
    )
    assert ROLL_MARKER not in body, (
        f"{page} says the annual declaration was postponed. Its two stored dates "
        f"disagree in this fixture and the page believed them — but art. 109 does not "
        f"move this deadline, so the sentence is telling a client she has days she "
        f"does not have"
    )


def test_the_annual_consequence_speaks_of_delivery_rather_than_payment(
    annual_firm: Firm,
) -> None:
    """A declaration is delivered, and its penalty applies in a year with no tax due."""
    body = _fetch(annual_firm, "portal-home")

    assert DASN_CONSEQUENCE in body, (
        f"{DASN_CONSEQUENCE!r} is absent; the annual card either says nothing about "
        f"the deadline or tells a client to PAY a declaration"
    )
    assert DAS_CONSEQUENCE not in body, (
        "the annual declaration is carrying the monthly guia's consequence line"
    )


def test_the_payments_page_postpones_the_monthly_row_and_not_the_annual_one(
    firm: Firm,
) -> None:
    """Both rows are on one page, and exactly one of them may claim it moved.

    Alpha carries a postponed DAS and a DASN whose stored dates disagree. Counting the
    occurrences is what makes this sharp: a template that dropped the type guard would
    render the sentence twice, and a template that dropped the roll note entirely would
    render it none.
    """
    body = _fetch(firm, "portal-payments")

    assert body.count(ROLL_MARKER) == 1, (
        f"{ROLL_MARKER!r} appears {body.count(ROLL_MARKER)} times on a page carrying "
        f"one postponed monthly guia and one annual declaration; it belongs to the "
        f"monthly row alone, because art. 109 does not move the annual deadline"
    )
    assert DASN_FIXED_DATE in body, (
        "the annual row states no fixed date, so the count above is a claim about a "
        "page that may simply be missing the annual row"
    )


# ------------------------------------------------------------- a next step, every time


def test_every_status_ends_on_something_the_client_can_do(firm: Firm) -> None:
    """A consequence with no next step leaves a person alarmed and stuck.

    Sibling coverage pins the badge word and the consequence sentence. This pins the
    third part, and reads the five statuses off `ObligationStatus` so a sixth added
    later reds here rather than rendering a card with nothing actionable on it.
    """
    body = _fetch(firm, "portal-payments")

    assert set(STATUS_NEXT_STEP) == set(ObligationStatus), (
        f"ObligationStatus defines {sorted(ObligationStatus.values)} and this case "
        f"knows a next step for "
        f"{sorted(status.value for status in STATUS_NEXT_STEP)}"
    )
    for status, next_step in STATUS_NEXT_STEP.items():
        assert next_step in body, (
            f"{status.value} offers no next step: {next_step!r} is absent, so the card "
            f"tells a client what the situation costs and nothing about what to do"
        )


# ------------------------------------------------------------- the stored words never


@pytest.mark.parametrize(
    ("page", "sentinel"),
    [
        ("portal-home", "Próxima ação"),
        ("portal-payments", "Guia mensal (DAS)"),
        ("portal-documents", "Arquivos enviados"),
        ("portal-account", "Alterar senha"),
    ],
)
def test_no_portal_page_shows_a_client_a_stored_status_word(
    firm: Firm,
    page: str,
    sentinel: str,
) -> None:
    """`scheduled`, `due`, `paid`, `overdue`, `waived` — none of them reaches a reader.

    These are the database's words. Rendering `{{ row.status }}`, or the display helper
    Django generates beside it, puts an English enum on the phone of someone this
    product was built to serve in Portuguese — and it does so while looking, in the
    template, exactly like rendering any other field.

    The scan runs over the STRIPPED text rather than the document, and matches whole
    words rather than substrings. The long comment beside `SCRIPT_OR_STYLE` sets out why
    both halves are needed; the short version is that `due` and `paid` live legitimately
    inside `datetime=` values and `badge-…` class names, and a scan that reds on those
    gets weakened until it means nothing.
    """
    text = _visible_text(_fetch(firm, page), sentinel=sentinel)

    assert _bare_status_tokens(text) == [], (
        f"{page} shows a client the stored status word(s) "
        f"{_bare_status_tokens(text)}. The portal maps every status onto a pt-BR word "
        f"and the sentence saying what it costs; a raw one means a branch fell through "
        f"to the column"
    )


def test_the_scan_would_notice_a_stored_word_if_one_were_there() -> None:
    """The control for the case above: prove the matcher matches.

    An absence assertion is only as good as the detector behind it, and this one strips
    most of its input away before looking. A stored word planted in ordinary prose has
    to be found, or "no page shows one" is a statement about a scan that finds nothing
    anywhere.
    """
    planted = (
        "<p class='badge badge-ok'>Paga</p>"
        "<time datetime='2027-03-22'>22/03/2027</time>"
        "<p>Situação: paid</p>"
    )

    tokens = _bare_status_tokens(_visible_text(planted, sentinel="Paga"))

    assert tokens == [ObligationStatus.PAID.value], (
        f"the scan found {tokens} in a fragment carrying exactly one planted status "
        f"word in its text and two decoys in its markup; either it misses the word or "
        f"it reds on the `datetime` attribute and the class name, and both make the "
        f"case above worthless"
    )


def test_no_portal_template_reaches_for_the_status_display_helper() -> None:
    """The structural half, covering the branches no fixture happens to render.

    `{{ row.get_status_display }}` reads like a courtesy and returns the model's own
    label — which for these five choices is the English word itself. A page can only be
    scanned where a fixture takes it; the source can be scanned everywhere.
    """
    offenders = [
        f"{path.relative_to(PORTAL_TEMPLATES).as_posix()}:{number}"
        for path in _portal_templates()
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        )
        if DISPLAY_HELPER in line
    ]

    assert offenders == [], (
        f"a portal template calls the status display helper: {offenders}. It returns "
        f"the model's own label, which for these choices is the stored English word — "
        f"the portal maps each status onto a pt-BR word and a consequence instead"
    )


# --------------------------------------------------------- numbers through the filters


def test_the_cnpj_is_punctuated_by_the_shared_filter(firm: Firm) -> None:
    """One filter decides what a CNPJ looks like, across both sides of this product.

    Asserted on the render AND on the source. The render proves the client sees a
    punctuated document rather than fourteen bare characters; the source proves the
    punctuation came from `cnpj_mask` rather than from a model property that happens to
    agree with it today.
    """
    body = _fetch(firm, "portal-home")

    assert cnpj_mask(ALPHA_CNPJ) in body, (
        f"{cnpj_mask(ALPHA_CNPJ)!r} is absent; the CNPJ is not rendered in the "
        f"official mask"
    )
    assert ALPHA_CNPJ not in body, (
        "the stored CNPJ appears unpunctuated, which is the database's spelling of it "
        "rather than the one on a Receita document"
    )

    source = HOME_TEMPLATE.read_text(encoding="utf-8")
    assert "|cnpj_mask" in source, (
        f"{HOME_TEMPLATE.name} does not pass the CNPJ through cnpj_mask; a second "
        f"formatter is a second answer to what a CNPJ looks like"
    )


def test_the_footer_promises_a_timezone_in_words_rather_than_in_an_iana_key(
    firm: Firm,
) -> None:
    """`America/Sao_Paulo` is correct, and it is a database key rather than a place."""
    body = _fetch(firm, "portal-home")

    assert TIMEZONE_PROMISE in body, f"{TIMEZONE_PROMISE!r} is absent from the footer"
    assert IANA_KEY not in body, (
        f"{IANA_KEY!r} is on a client's screen; it is an identifier this product uses "
        f"internally, and the portal says the same thing in words a person uses"
    )
