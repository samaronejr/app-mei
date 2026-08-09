"""The firm's own screens never show an accountant the database's words either.

`tests/portal/test_portal_copy.py` refuses the five stored `ObligationStatus` values on
the four pages a MEI owner reads. This module is the same refusal, aimed at the seven
pages her ACCOUNTANT reads -- and the reason it is a separate guard rather than a
widened one is that the two sides leak through different holes. The portal was written
against `{{ row.status }}`; the firm side reaches for `{{ row.get_status_display }}`,
which reads like a courtesy, returns the model's own label, and for these choices the
label IS the stored English word.

Four enums reach a firm screen and all four are refused here:

* `ClientStatus` -- `onboarding`, `active`, `suspended`, `closed` -- on the dashboard's
  Carteira tiles, in the registry's SITUAÇÃO badge and on the client workspace's
  identity card;
* `ObligationStatus` -- `scheduled`, `due`, `paid`, `overdue`, `waived` -- in the two
  obligation queues' rows, in their status filter, and in the workspace's obligation
  table -- which is the only surface in the product where all five are reachable on
  ROWS, because the queues exclude the settled two outright;
* `OnboardingStatus` -- `pending`, `blocked`, `done`, `not_applicable` -- in the
  onboarding queue's SITUAÇÃO cell and in the workspace's checklist badges;
* `GovBrTrustLevel` -- but only `unknown`, and the exclusion of the other three is
  DERIVED rather than listed. `bronze`, `prata` and `ouro` are stored values that are
  already the Portuguese word a reader expects, so refusing them would red on the copy
  this product ships; `unknown` is the one tier whose stored value is English, and it
  is refused for exactly that reason. `GOVBR_ENGLISH_TOKENS` computes the split from
  the approved words, so a fifth tier named in English is refused the day it lands.

The third is here because the surface is here. `templates/obligations/_rows_onboarding
.html` renders an `OnboardingItem`, not a client, so a `ClientStatus` map placed on it
would match nothing and fall through to the English label it was meant to replace -- a
fix that changes the template and not the screen. The enum that is actually on the row
is the enum that gets mapped, and the enum that gets mapped is the enum this scan
refuses.

**SHAPE**: markup is stripped FIRST, and only then is each token matched with word
boundaries against what is left. Both halves are load-bearing and the long note beside
`SCRIPT_OR_STYLE` in the portal module sets out why; the short version is that the raw
codes appear LEGITIMATELY in this project's markup -- the `data-portfolio-…` attribute
on every dashboard tile, the badge-class conditional at `templates/clients/list.html`
which Tailwind needs written out as literals, and the `value="…"` of every status filter
option, which is the parameter the view filters on. Stripping already excludes all
three, and a scan that reddened on them would be weakened until it meant nothing.

**One stored value is deliberately NOT refused, and saying so is the honest half of
this module.** `onboarding` is a loanword this product has adopted into its own firm-
side vocabulary in three places that have nothing to do with a status column: the
navigation item is called `Onboarding` (`apps/core/navigation.py:51`, so the word is on
EVERY firm page), the queue is called "Onboarding travado", and the Carteira tile this
change installs reads "Em onboarding". Refusing the token would red the chrome of every
page in the product, and a guard that reds on its own navigation is one somebody
weakens until it means nothing.

That branch is guarded the other way round instead, which for this one value is sharper
than a ban: `test_the_carteira_tiles_name_every_client_status_in_portuguese` and
`test_the_registry_badge_names_every_client_status_in_portuguese` REQUIRE "Em
onboarding" on the two surfaces that render it, so a fall-through printing the bare
column value fails for saying the wrong thing rather than for saying a banned thing --
and it also catches a cell that renders nothing at all, which no absence assertion can.
`test_the_excluded_token_is_chrome_rather_than_a_status_cell` pins the reason for the
exclusion against a page that renders no client status at all.
"""

import datetime as dt
import html
import re
from dataclasses import dataclass, replace
from decimal import Decimal
from http import HTTPStatus
from pathlib import Path
from typing import Final

import pytest
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.clients.models import (
    ClientCompany,
    ClientStatus,
    GovBrTrustLevel,
    OnboardingItem,
    OnboardingStatus,
)
from apps.core.tenancy import tenant_context
from apps.obligations.models import (
    MonthlyRevenue,
    Obligation,
    ObligationStatus,
    ObligationType,
)
from tests.ui.factories import Firm, add_client, client_base, make_firm

pytestmark = pytest.mark.django_db(transaction=True)

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
FIRM_TEMPLATES: Final[Path] = PROJECT_ROOT / "templates"

# --------------------------------------------------------------------- the pt-BR words
#
# The four client words are this module's own. The five obligation words are NOT: they
# are lifted verbatim from `tests/portal/test_portal_payments.py:127-148`, where the
# closed plan pinned them for the client-facing page. A firm and its client must read
# the same word for the same row -- an accountant who says "está paga" to somebody whose
# screen says something else is debugging the product mid-call.

CLIENT_STATUS_WORDS: Final[dict[str, str]] = {
    ClientStatus.ONBOARDING: "Em onboarding",
    ClientStatus.ACTIVE: "Ativo",
    ClientStatus.SUSPENDED: "Suspenso",
    ClientStatus.CLOSED: "Encerrado",
}

OBLIGATION_STATUS_WORDS: Final[dict[str, str]] = {
    ObligationStatus.SCHEDULED: "Programada",
    ObligationStatus.DUE: "A pagar",
    ObligationStatus.PAID: "Paga",
    ObligationStatus.OVERDUE: "Em atraso",
    ObligationStatus.WAIVED: "Dispensada",
}

ONBOARDING_STATUS_WORDS: Final[dict[str, str]] = {
    OnboardingStatus.PENDING: "Pendente",
    OnboardingStatus.BLOCKED: "Travado",
    OnboardingStatus.DONE: "Concluído",
    OnboardingStatus.NOT_APPLICABLE: "Não se aplica",
}

# The gov.br tier, which is on the workspace's identity card beside the client status.
# `Não informado` rather than `Desconhecido`: the latter is what
# `apps/core/templatetags/ptbr.py` renders for a value no map knows, so reusing it here
# would make "nobody ever asked this client" and "the enum grew a member nobody mapped"
# read identically -- and the second is a defect the first would then hide.
GOVBR_TRUST_WORDS: Final[dict[str, str]] = {
    GovBrTrustLevel.UNKNOWN: "Não informado",
    GovBrTrustLevel.BRONZE: "Bronze",
    GovBrTrustLevel.PRATA: "Prata",
    GovBrTrustLevel.OURO: "Ouro",
}

# Only three of the five obligation words can appear on a QUEUE row: `due_soon` and
# `overdue` both build on `_unsettled`, which excludes PAID and WAIVED outright
# (`apps/obligations/queries.py:53,104`). The other two are reachable through the
# status FILTER, whose options are `ObligationStatus.choices` in full -- which is why
# the filter is asserted separately below rather than folded into the row case.
QUEUE_ROW_STATUSES: Final[tuple[str, ...]] = (
    ObligationStatus.SCHEDULED,
    ObligationStatus.DUE,
    ObligationStatus.OVERDUE,
)

# ------------------------------------------------------------------ the enum-leak scan

SCRIPT_OR_STYLE: Final[re.Pattern[str]] = re.compile(
    r"<(script|style)\b.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
HTML_COMMENT: Final[re.Pattern[str]] = re.compile(r"<!--.*?-->", re.DOTALL)
TAG: Final[re.Pattern[str]] = re.compile(r"<[^>]*>")
RUN_OF_SPACE: Final[re.Pattern[str]] = re.compile(r"\s+")

# The stored VALUES, and deliberately not the labels beside them.
#
# The portal's scan refuses both, and it can: every `ObligationStatus` label is the
# English value verbatim, so the two sets coincide there. On this side they do not.
# `ClientStatus.ACTIVE` is declared `_("active")` and Django's BUNDLED catalog already
# translates that msgid, so `str(ClientStatus.ACTIVE.label)` is `ativo` under
# `LANGUAGE_CODE = "pt-br"` -- which is simultaneously "the label Django generates" and
# the correct Portuguese word for the tile. A label-based ban would therefore refuse the
# very copy the product is supposed to show, and a guard that reds on its own answer is
# a guard somebody weakens until it means nothing.
#
# Nothing is lost by narrowing, because the labels were only ever a proxy for one
# defect -- a template reaching for `get_status_display` -- and that defect is refused
# STRUCTURALLY below, over the source rather than over a render. The structural half is
# strictly stronger than the proxy it replaces: it covers every branch, including the
# ones no fixture reaches, and it catches `not applicable` (a label whose value,
# `not_applicable`, is spelled differently) which no text scan here would.
# The product's own navigation word, and therefore the one stored value no firm page
# can be scanned for. The module docstring sets out why at length, and
# `test_the_excluded_token_is_chrome_rather_than_a_status_cell` proves the premise
# instead of asserting it in prose.
EXCLUDED_TOKEN: Final[str] = str(ClientStatus.ONBOARDING.value)

# A gov.br tier whose stored value already IS its Portuguese word is not a leak, and
# refusing it would red on copy the product is required to render. The split is DERIVED
# from the approved words rather than listed, which is what makes it self-maintaining:
# a tier added in English is refused on the day it lands, and one added in Portuguese is
# excluded without anybody editing this file. `.get(member, "")` is the conservative
# arm -- a member nobody has written a word for can never equal its own value, so it is
# refused rather than quietly admitted, and
# `test_the_gov_br_words_cover_every_tier_the_column_can_hold` is what names it.
GOVBR_ENGLISH_TOKENS: Final[frozenset[str]] = frozenset(
    str(member.value)
    for member in GovBrTrustLevel
    if str(member.value).casefold() != GOVBR_TRUST_WORDS.get(member, "").casefold()
)

STORED_STATUS_TOKENS: Final[frozenset[str]] = (
    frozenset(
        {str(member.value) for member in ClientStatus}
        | {str(member.value) for member in ObligationStatus}
        | {str(member.value) for member in OnboardingStatus},
    )
    - {EXCLUDED_TOKEN}
) | GOVBR_ENGLISH_TOKENS

# The templates that compose the seven pages scanned here, named rather than discovered.
# `templates/clients/detail.html` and `templates/clients/_identity.html` were once
# recorded here as a deliberate omission -- the client workspace was a screen owned by
# neither this scan nor the change that wrote it. **That omission is now CLOSED**: both
# templates are in this set, the workspace is a scanned surface below, and no firm-side
# template calls the display helper anywhere in the product.
SCANNED_TEMPLATES: Final[tuple[str, ...]] = (
    "core/dashboard.html",
    "clients/list.html",
    "clients/detail.html",
    "clients/_identity.html",
    "obligations/queue.html",
    "obligations/_queue.html",
    "obligations/_counts.html",
    "obligations/_rows_obligations.html",
    "obligations/_rows_onboarding.html",
    "obligations/_rows_threshold.html",
)

DISPLAY_HELPER: Final[str] = "get_status_display"

# A `{% comment %}` block, blanked before the structural scan below. Django emits
# nothing for one, so a helper NAMED inside a comment cannot reach a screen -- it is
# prose about markup rather than markup, exactly the distinction
# `tests/ui/test_resilience.py:90-97` already draws for `hx-*` attributes. This is the
# opposite of what `templates_scan.class_tokens` does, and deliberately: a class name
# in a comment still has to be compiled by Tailwind, so that scan reads them on purpose.
COMMENT_BLOCK: Final[re.Pattern[str]] = re.compile(
    r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}",
    re.DOTALL,
)

# ------------------------------------------------------------------------ the surfaces


@dataclass(frozen=True)
class Surface:
    """One firm page, and everything needed to prove a scan of it means something.

    `sentinel` must survive the stripper, `empty_state` must be absent from what it
    leaves, `row_client` names a client whose row has to be on the page, and
    `raw_markers` are checked against the UNSTRIPPED document -- they are attributes, so
    stripping is exactly what would throw them away.

    `targets_row_client` makes `row_client` do double duty on the one surface that is
    not a collection: the client workspace is reversed WITH that client's primary key,
    so the row the page must carry and the record the URL names are the same client by
    construction rather than by two constants agreeing.
    """

    url_name: str
    sentinel: str
    empty_state: str
    row_client: str
    raw_markers: tuple[str, ...] = ()
    targets_row_client: bool = False


# Every tile is rendered whatever the tally, so all four are demanded: a Carteira
# missing a tile is a reader who cannot tell a zero from a status nobody thought about
# (`apps/core/dashboard.py:74-88`).
PORTFOLIO_TILES: Final[tuple[str, ...]] = tuple(
    f"data-portfolio-{status}" for status in CLIENT_STATUS_WORDS
)

# The status filter, which only the two obligation queues carry (`accepts_status` is
# False on the other two by construction, `apps/obligations/views.py:113-131`).
STATUS_FILTER_MARKER: Final[str] = 'id="situacao"'

QUEUE_EMPTY_STATE: Final[str] = "Nenhum item nesta fila."

WORKSPACE: Final[str] = "client-detail"

# The onboarding checklist's own container, checked against the raw document because
# the workspace's second table has an empty state of its own that `Surface.empty_state`
# has no room for -- one gate per surface, and the obligation table already holds it.
CHECKLIST_MARKER: Final[str] = 'class="checklist"'

SURFACES: Final[tuple[Surface, ...]] = (
    Surface(
        "dashboard",
        sentinel="Carteira",
        empty_state="Nenhum cliente perto do limite.",
        row_client="active",
        raw_markers=PORTFOLIO_TILES,
    ),
    Surface(
        "clients-list",
        sentinel="Razão social",
        empty_state="Nenhum cliente encontrado.",
        # The suspended client rather than the active one: it is the branch a registry
        # showing only healthy rows would silently drop, and the badge chain's non-
        # default arms are the ones a fall-through hides in.
        row_client="suspended",
    ),
    Surface(
        "queue-due-soon",
        sentinel="Vencendo em 7 dias",
        empty_state=QUEUE_EMPTY_STATE,
        row_client="active",
        raw_markers=(STATUS_FILTER_MARKER,),
    ),
    Surface(
        "queue-overdue",
        sentinel="Atrasadas",
        empty_state=QUEUE_EMPTY_STATE,
        row_client="active",
        raw_markers=(STATUS_FILTER_MARKER,),
    ),
    Surface(
        "queue-onboarding",
        sentinel="Onboarding travado",
        empty_state=QUEUE_EMPTY_STATE,
        row_client="active",
    ),
    Surface(
        "queue-threshold",
        sentinel="Limite de faturamento",
        empty_state=QUEUE_EMPTY_STATE,
        row_client="active",
    ),
    Surface(
        WORKSPACE,
        sentinel="Identificação",
        empty_state="Nenhuma obrigação gerada para este cliente ainda.",
        row_client="active",
        raw_markers=(CHECKLIST_MARKER,),
        targets_row_client=True,
    ),
)

SURFACE_BY_NAME: Final[dict[str, Surface]] = {
    surface.url_name: surface for surface in SURFACES
}

# ------------------------------------------------------------------------- the fixture

SLUG: Final[str] = "copia"

# Legal names carry no English at all, so a row name can never be mistaken for a leaked
# code by a scan that matches whole words case-insensitively.
ACTIVE_NAME: Final[str] = "Alfa Servicos MEI"
ONBOARDING_NAME: Final[str] = "Beta Comercio MEI"
SUSPENDED_NAME: Final[str] = "Gama Transportes MEI"
CLOSED_NAME: Final[str] = "Delta Oficina MEI"

DAS_CODE: Final[str] = "DAS"

DUE_SOON_DAYS: Final[int] = 2
DUE_SOON_SECOND_DAYS: Final[int] = 3
OVERDUE_DAYS: Final[int] = -9
OVERDUE_SECOND_DAYS: Final[int] = -12

# Below the MEI ceiling and close enough to it to land in an attention band, so the
# threshold queue and the dashboard's own threshold table both have a row.
NEAR_CEILING: Final[Decimal] = Decimal("75000.00")

BLOCKED_REASON: Final[str] = "aguardando procuração"

# Which tier each seeded client's gov.br column holds. Four clients, four tiers, one
# each: the workspace renders exactly one tier per page, so covering the enum means
# spreading it across the four clients rather than fetching one page four times.
# `test_the_gov_br_words_cover_every_tier_the_column_can_hold` refuses a mapping that
# stopped being a bijection.
GOVBR_TIER_BY_CLIENT: Final[dict[str, str]] = {
    ClientStatus.ONBOARDING: GovBrTrustLevel.UNKNOWN,
    ClientStatus.ACTIVE: GovBrTrustLevel.OURO,
    ClientStatus.SUSPENDED: GovBrTrustLevel.BRONZE,
    ClientStatus.CLOSED: GovBrTrustLevel.PRATA,
}

# Far enough out to sit outside the 7-day horizon and nowhere near overdue, so the row
# each non-active client needs to keep its workspace off the empty state cannot also
# appear in a queue and change what the other six surfaces are scanning.
QUIET_DUE_DAYS: Final[int] = 90

# The checklist statuses the workspace must show, assigned to the first items of the
# seeded list by position. The blocked one stays first because `_stock` reads
# `.first()` for the onboarding queue's own row.
CHECKLIST_COVERAGE: Final[tuple[str, ...]] = (
    OnboardingStatus.BLOCKED,
    OnboardingStatus.DONE,
    OnboardingStatus.NOT_APPLICABLE,
    OnboardingStatus.PENDING,
)


@dataclass(frozen=True)
class Portfolio:
    """A firm whose every scanned page has real rows on it."""

    firm: Firm
    active: ClientCompany
    onboarding: ClientCompany
    suspended: ClientCompany
    closed: ClientCompany

    def name_of(self, key: str) -> str:
        """Return the legal name of the client a surface demands a row for."""
        company: ClientCompany = getattr(self, key)
        return str(company.legal_name)

    def pk_of(self, key: str) -> str:
        """Return the primary key the workspace URL for that client is built from."""
        company: ClientCompany = getattr(self, key)
        return str(company.pk)


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]


def _competence(months_back: int) -> dt.date:
    """Return a distinct first-of-month, so no two seeded rows collide on a period."""
    today = dt.date.today()  # noqa: DTZ011 - a competence label, never a deadline
    total = today.year * 12 + (today.month - 1) - months_back
    return dt.date(total // 12, total % 12 + 1, 1)


def _stock(portfolio: Portfolio) -> None:
    """Give the active client work in every queue this module scans.

    Nearly all of it hangs off ONE client on purpose: each queue's row gate then names
    the same company, so a scoping regression that emptied a queue reds as a missing row
    rather than as a page that merely happened to have nothing on it.

    The settled pair is the exception, and it is the whole reason the workspace is
    scanned. `due_soon` and `overdue` both build on `_unsettled`, which excludes PAID
    and WAIVED outright (`apps/obligations/queries.py:53,104`), so no queue ROW can ever
    carry either word. The workspace's obligation table applies no such exclusion, which
    makes it the one page in the product where all five reach a reader as ROWS rather
    than as filter options.
    """
    firm = portfolio.firm
    das = ObligationType.objects.get(code=DAS_CODE)
    today = dt.date.today()  # noqa: DTZ011 - matched to the queues' own `localdate()`
    seeded = (
        (DUE_SOON_DAYS, ObligationStatus.SCHEDULED, 0),
        (DUE_SOON_SECOND_DAYS, ObligationStatus.DUE, 1),
        (OVERDUE_DAYS, ObligationStatus.OVERDUE, 2),
        (OVERDUE_SECOND_DAYS, ObligationStatus.SCHEDULED, 3),
        (QUIET_DUE_DAYS, ObligationStatus.PAID, 4),
        (QUIET_DUE_DAYS, ObligationStatus.WAIVED, 5),
    )
    with tenant_context(firm.tenant.id):
        for offset, status, index in seeded:
            Obligation.objects.create(
                tenant=firm.tenant,
                client=portfolio.active,
                obligation_type=das,
                competence_month=_competence(index),
                nominal_due_date=today + dt.timedelta(days=offset),
                resolved_due_date=today + dt.timedelta(days=offset),
                status=status,
            )
        for company in (portfolio.onboarding, portfolio.suspended, portfolio.closed):
            Obligation.objects.create(
                tenant=firm.tenant,
                client=company,
                obligation_type=das,
                competence_month=_competence(0),
                nominal_due_date=today + dt.timedelta(days=QUIET_DUE_DAYS),
                resolved_due_date=today + dt.timedelta(days=QUIET_DUE_DAYS),
                status=ObligationStatus.SCHEDULED,
            )
        MonthlyRevenue.objects.create(
            tenant=firm.tenant,
            client=portfolio.active,
            competence_month=dt.date(today.year, 1, 1),
            gross_amount=NEAR_CEILING,
        )
        _mark_checklist(portfolio.active)


def _mark_checklist(company: ClientCompany) -> None:
    """Put a different `OnboardingStatus` on each of the first four checklist items.

    One badge renders one status, so a checklist left at its seeded default reaches one
    arm of a four-arm chain -- and an absence over the other three is an absence from a
    branch nobody rendered.
    """
    items = list(
        OnboardingItem.objects.filter(client=company).order_by("position", "key", "pk"),
    )
    assert len(items) >= len(CHECKLIST_COVERAGE), (
        f"{company.legal_name} has {len(items)} checklist items and this module needs "
        f"{len(CHECKLIST_COVERAGE)} to reach every OnboardingStatus badge, so the "
        f"workspace scan would quantify over arms nothing rendered"
    )
    for item, status in zip(items, CHECKLIST_COVERAGE, strict=False):
        item.status = status
        item.blocked_reason = (
            BLOCKED_REASON if status == OnboardingStatus.BLOCKED else ""
        )
        item.save(update_fields=["status", "blocked_reason"])


@pytest.fixture
def portfolio() -> Portfolio:
    """Four clients, one per `ClientStatus`, and a book that fills all four queues.

    Every status is seeded because the registry's badge is a four-arm chain and the
    Carteira is four tiles: a fixture holding only active clients reaches one arm, and
    an absence over the other three is an absence from a branch nobody rendered.
    """
    firm = make_firm(SLUG)
    companies = [
        add_client(firm, legal_name=name, base=client_base(index, SLUG), status=status)
        for index, (name, status) in enumerate(
            (
                (ACTIVE_NAME, ClientStatus.ACTIVE),
                (ONBOARDING_NAME, ClientStatus.ONBOARDING),
                (SUSPENDED_NAME, ClientStatus.SUSPENDED),
                (CLOSED_NAME, ClientStatus.CLOSED),
            ),
        )
    ]
    with tenant_context(firm.tenant.id):
        for company in companies:
            company.govbr_trust_level = GOVBR_TIER_BY_CLIENT[company.status]
            company.save(update_fields=["govbr_trust_level"])
    built = Portfolio(firm, *companies)
    _stock(built)
    return built


# --------------------------------------------------------------------------- helpers


def _fetch(portfolio: Portfolio, surface: Surface) -> str:
    """GET one firm page as the owner, refusing anything that is not a rendered page.

    The status gate is the first non-vacuity control: a redirect, a 403 or a 404 returns
    a body that satisfies every "…is absent" assertion below it perfectly. It matters
    twice as much on the workspace, whose view answers 404 for a client outside the
    reader's portfolio rather than 403 -- so a scoping regression there would otherwise
    read as a page with no English on it.
    """
    target = (
        str(reverse(surface.url_name, args=[portfolio.pk_of(surface.row_client)]))
        if surface.targets_row_client
        else str(reverse(surface.url_name))
    )
    response = portfolio.firm.as_owner().get(target)
    assert response.status_code == HTTPStatus.OK, (
        f"{surface.url_name} answered {response.status_code}, so nothing asserted "
        f"about it is a statement about a rendered page"
    )
    document = str(response.content.decode())
    for marker in surface.raw_markers:
        assert marker in document, (
            f"{surface.url_name} does not carry {marker!r}, so the surface this scan "
            f"exists to cover was not rendered at all"
        )
    return document


def _readable(document: str, surface: Surface, portfolio: Portfolio) -> str:
    """Return only what an accountant sees, with every gate that makes that meaningful.

    Four gates, and all four live in here rather than in tests of their own so that
    `pytest -k` cannot deselect one and leave the scan quantifying over an empty string:
    the stripped text is non-empty, the page's own heading survived the stripper, the
    page is not showing its empty state, and the row a surface promises is really there.
    """
    without_code = HTML_COMMENT.sub(" ", SCRIPT_OR_STYLE.sub(" ", document))
    text = RUN_OF_SPACE.sub(" ", html.unescape(TAG.sub(" ", without_code))).strip()

    assert text, (
        "stripping the markup left no text at all, so every token this scan refuses "
        "is absent from an empty string"
    )
    assert surface.sentinel in text, (
        f"{surface.sentinel!r} is missing from {surface.url_name}'s stripped text, so "
        f"the stripper threw the page's own content away and every absence below is an "
        f"absence from the wrong string"
    )
    assert surface.empty_state not in text, (
        f"{surface.url_name} is showing {surface.empty_state!r}: the page rendered its "
        f"empty branch, and an empty list contains no status word for any reason at all"
    )
    expected_row = portfolio.name_of(surface.row_client)
    assert expected_row in text, (
        f"{expected_row!r} is not on {surface.url_name}, so the rows whose status "
        f"cells this scan reads were never rendered"
    )
    return text


def _leaked_status_words(text: str) -> list[str]:
    """Return every stored status word appearing as a WORD in what a reader sees."""
    return sorted(
        token
        for token in STORED_STATUS_TOKENS
        if re.search(rf"\b{re.escape(token)}\b", text, re.IGNORECASE)
    )


def _template_source(name: str) -> str:
    """Return one firm template's raw source, refusing a path that is not there.

    A missing file would read as a template with no offending line in it, which is how
    a structural scan quietly stops covering the thing it was named after.
    """
    path = FIRM_TEMPLATES / name
    assert path.is_file(), (
        f"{path} does not exist, so a scan quantified over it would find no offender "
        f"for the same reason an empty file has none"
    )
    return path.read_text(encoding="utf-8")


def _markup_only(source: str) -> str:
    """Return the template with every `{% comment %}` block blanked, line count intact.

    The newlines are kept rather than collapsed so an offender is still reported at the
    line it is actually on -- a scan that named the wrong line would send the next
    reader to a `<td>` that looks innocent.
    """
    return COMMENT_BLOCK.sub(lambda block: "\n" * block.group(0).count("\n"), source)


def _scan_surface(portfolio: Portfolio, surface: Surface) -> tuple[str, list[str]]:
    """Fetch one surface and return its readable text beside whatever leaked."""
    text = _readable(_fetch(portfolio, surface), surface, portfolio)
    return text, _leaked_status_words(text)


def _scan(portfolio: Portfolio, url_name: str) -> tuple[str, list[str]]:
    """Fetch one named surface and return its readable text beside whatever leaked."""
    return _scan_surface(portfolio, SURFACE_BY_NAME[url_name])


def _workspace(key: str) -> Surface:
    """Return the workspace surface aimed at one seeded client rather than the default.

    The Portfolio attribute names ARE the `ClientStatus` values, so a status picks a
    page: `_workspace(ClientStatus.SUSPENDED)` opens the suspended client's own screen
    and demands its own legal name on it.
    """
    return replace(SURFACE_BY_NAME[WORKSPACE], row_client=key)


# ----------------------------------------------------------- the stored words, never


@pytest.mark.parametrize("url_name", [surface.url_name for surface in SURFACES])
def test_no_firm_page_shows_an_accountant_a_stored_status_word(
    portfolio: Portfolio,
    url_name: str,
) -> None:
    """The database's words never reach the screen, on any of the six firm pages.

    `get_status_display` is the hole this closes. It reads like a courtesy and returns
    the model's own label, which for all three of these enums IS the stored English
    word -- so a template calling it puts `active`, `overdue` or `blocked` in front of
    an accountant while looking, in the source, exactly like rendering any other field.
    """
    _, leaked = _scan(portfolio, url_name)

    assert leaked == [], (
        f"{url_name} shows an accountant the stored status word(s) {leaked}. Every "
        f"firm surface maps its status onto a pt-BR word with the raw code kept only "
        f"as the fallback; a raw one on screen means a branch fell through to the "
        f"column"
    )


def test_the_scan_would_notice_a_stored_word_if_one_were_there() -> None:
    """The control for the case above: prove the matcher matches.

    An absence assertion is only as good as the detector behind it, and this one strips
    most of its input away and then deletes two phrases from what is left. A stored word
    planted in ordinary prose has to be found, or "no page shows one" is a statement
    about a scan that finds nothing anywhere.
    """
    planted = (
        "<span class='badge badge-ok'>Ativo</span>"
        "<div class='metric' data-portfolio-suspended='3'>"
        "<time datetime='2027-03-22'>22/03/2027</time>"
        "<p>Situação: active</p></div>"
    )

    tokens = _leaked_status_words(
        RUN_OF_SPACE.sub(" ", html.unescape(TAG.sub(" ", planted))).strip(),
    )

    assert tokens == [str(ClientStatus.ACTIVE.value)], (
        f"the scan found {tokens} in a fragment carrying exactly one planted status "
        f"word in its text and three decoys in its markup; either it misses the word "
        f"or it reds on the data attribute, the class name and the `datetime` value, "
        f"and all four make the case above worthless"
    )


def test_no_scanned_firm_template_reaches_for_the_status_display_helper() -> None:
    """The structural half, covering the branches no fixture happens to render.

    `{{ row.get_status_display }}` reads like a courtesy and returns the model's own
    label, which for these three enums is the stored English word for every choice but
    one. A page can only be scanned where a fixture takes it; the source can be scanned
    everywhere, and this is also what refuses `not applicable` -- a label spelled
    differently from its value, which the text scan above could never name.

    `templates/clients/detail.html` and `templates/clients/_identity.html` were outside
    this set and DID still call the helper -- three times between them, for the client's
    status, its obligations and its checklist. That was recorded here as a deliberate
    boundary rather than left silent, and **the boundary is now closed**: both templates
    are scanned, the workspace is a rendered surface above, and no firm-side template in
    this product reaches for the helper anywhere.
    """
    offenders = [
        f"{name}:{number}"
        for name in SCANNED_TEMPLATES
        for number, line in enumerate(
            _markup_only(_template_source(name)).splitlines(),
            start=1,
        )
        if DISPLAY_HELPER in line
    ]

    assert offenders == [], (
        f"a scanned firm template calls the status display helper: {offenders}. It "
        f"returns the model's own label, which for these choices is the stored English "
        f"word; every firm surface maps the status onto a pt-BR word instead and keeps "
        f"the raw code only as the fallback"
    )


def test_the_structural_scan_reads_markup_and_not_the_prose_beside_it() -> None:
    """The control for the blanking step: it must hide comments and nothing else.

    Every template in this tree explains itself in a `{% comment %}` block, and the
    ones changed here explain why they stopped calling the helper -- which means naming
    it. Blanking is therefore required, and a blanking step is a hole unless something
    proves the call is still found where it can actually render.
    """
    in_markup = "<td>{{ row.get_status_display }}</td>"
    in_prose = (
        "{% comment %}\n  replaced {{ row.get_status_display }} here\n{% endcomment %}"
    )

    assert DISPLAY_HELPER in _markup_only(in_markup), (
        "the blanking step hid a call written in ordinary markup, so the structural "
        "case above quantifies over nothing and passes for the wrong reason"
    )
    assert DISPLAY_HELPER not in _markup_only(in_prose), (
        "the blanking step left a call named inside a comment, so every template that "
        "explains why it stopped calling the helper reds for saying so"
    )
    assert _markup_only(in_prose).count("\n") == in_prose.count("\n"), (
        "blanking changed the line count, so an offender would be reported at a line "
        "it is not on"
    )


def test_no_word_this_product_ships_is_refused_by_its_own_scan() -> None:
    """The scan may not red on the copy the product is supposed to render.

    This is the case that caught the trap. `ClientStatus.ACTIVE`'s label resolves to
    `ativo` through Django's bundled catalog, so an earlier draft of this module banned
    the correct Portuguese word for the tile and would have made the fix unshippable
    while looking, in the source, like an ordinary tightening.
    """
    shipped = {
        *CLIENT_STATUS_WORDS.values(),
        *OBLIGATION_STATUS_WORDS.values(),
        *ONBOARDING_STATUS_WORDS.values(),
        *GOVBR_TRUST_WORDS.values(),
    }

    refused = {word: _leaked_status_words(word) for word in sorted(shipped)}
    offenders = {word: tokens for word, tokens in refused.items() if tokens}

    assert offenders == {}, (
        f"this module refuses {offenders}, which is copy it also requires the firm "
        f"pages to render. A guard that reds on its own expected answer is one "
        f"somebody weakens until it means nothing"
    )


def test_the_excluded_token_is_chrome_rather_than_a_status_cell(
    portfolio: Portfolio,
) -> None:
    """`onboarding` is the one stored value not refused, and this is the proof of why.

    The threshold queue renders no `ClientStatus` anywhere -- its rows are revenue
    bands -- and the word is on it regardless, because the navigation item beside it is
    called `Onboarding` and the queue after that is called `Onboarding travado`. So the
    token is this product's own chrome on every firm page, and refusing it would red
    six pages for their menu rather than for a leak.

    Asserted against a live render rather than written down, so that a day when the
    navigation stops shipping the word is a day this exclusion gets looked at again.
    """
    text, leaked = _scan(portfolio, "queue-threshold")

    assert EXCLUDED_TOKEN not in STORED_STATUS_TOKENS, (
        f"{EXCLUDED_TOKEN!r} is being refused after all, which reds every firm page "
        f"for its own navigation item"
    )
    assert EXCLUDED_TOKEN in text.lower(), (
        f"{EXCLUDED_TOKEN!r} is no longer on a page that renders no client status at "
        f"all, so the reason it is excluded from {sorted(STORED_STATUS_TOKENS)} no "
        f"longer holds and the exclusion should be reconsidered"
    )
    assert leaked == [], (
        f"the threshold queue leaked {leaked}; it renders revenue bands and no status "
        f"column, so anything here comes from the shell every firm page shares"
    )


# --------------------------------------------------------- and the pt-BR words, always
#
# The scan above is an ABSENCE, and an absence is satisfied by a cell that renders
# nothing at all. Each case below names the word that has to be in its place, so a
# template whose chain fell through to an empty string cannot pass by leaking less.


def test_the_carteira_tiles_name_every_client_status_in_portuguese(
    portfolio: Portfolio,
) -> None:
    """All four tiles render whatever the tally, so all four words must be there."""
    text, _ = _scan(portfolio, "dashboard")

    missing = [word for word in CLIENT_STATUS_WORDS.values() if word not in text]
    assert missing == [], (
        f"the dashboard's Carteira is missing {missing}. Every status renders a tile "
        f"whatever its count (apps/core/dashboard.py:74-88), so a missing word is a "
        f"tile that fell through its chain rather than a status nobody has"
    )


def test_the_registry_badge_names_every_client_status_in_portuguese(
    portfolio: Portfolio,
) -> None:
    """One client per status is seeded, so all four badge arms are on this page."""
    text, _ = _scan(portfolio, "clients-list")

    for name in (ACTIVE_NAME, ONBOARDING_NAME, SUSPENDED_NAME, CLOSED_NAME):
        assert name in text, (
            f"{name!r} is not in the registry, so the badge arm belonging to it was "
            f"never rendered and the word asserted for it proves nothing"
        )
    missing = [word for word in CLIENT_STATUS_WORDS.values() if word not in text]
    assert missing == [], (
        f"the registry's SITUAÇÃO column is missing {missing}; one client per status "
        f"is on this page, so each arm of the badge chain rendered and a missing word "
        f"is an arm that fell through"
    )


@pytest.mark.parametrize("url_name", ["queue-due-soon", "queue-overdue"])
def test_the_status_filter_offers_all_five_obligation_words_in_portuguese(
    portfolio: Portfolio,
    url_name: str,
) -> None:
    """The filter is the only surface where all five words are reachable at once.

    Its options are `ObligationStatus.choices` in full (`apps/obligations/views.py`),
    including the two a queue can never show, so a chain missing an arm shows up here
    even though no row could ever reach it.
    """
    text, _ = _scan(portfolio, url_name)

    missing = [word for word in OBLIGATION_STATUS_WORDS.values() if word not in text]
    assert missing == [], (
        f"{url_name}'s Situação filter is missing {missing}. The options are the whole "
        f"enum, so a missing word is an option rendering its raw code or nothing"
    )


@pytest.mark.parametrize("url_name", ["queue-due-soon", "queue-overdue"])
def test_every_obligation_row_states_its_status_in_portuguese(
    portfolio: Portfolio,
    url_name: str,
) -> None:
    """The three statuses a queue row can carry, each on the screen as a pt-BR word."""
    text, _ = _scan(portfolio, url_name)

    present = [
        OBLIGATION_STATUS_WORDS[status]
        for status in QUEUE_ROW_STATUSES
        if OBLIGATION_STATUS_WORDS[status] in text
    ]
    assert present, (
        f"{url_name} states none of "
        f"{[OBLIGATION_STATUS_WORDS[s] for s in QUEUE_ROW_STATUSES]} anywhere, so its "
        f"rows carry a Situação cell with no word in it"
    )


def test_the_onboarding_queue_row_states_its_status_in_portuguese(
    portfolio: Portfolio,
) -> None:
    """The blocked item's cell reads `Travado`, agreeing with the queue's own name.

    This row is an `OnboardingItem` and its column is `OnboardingStatus` -- not the
    client's status, which is what makes it the surface a `ClientStatus` map would miss
    entirely while appearing to have fixed it.
    """
    text, _ = _scan(portfolio, "queue-onboarding")

    assert BLOCKED_REASON in text, (
        f"{BLOCKED_REASON!r} is absent, so the blocked item this case reads its status "
        f"cell from is not on the page"
    )
    assert ONBOARDING_STATUS_WORDS[OnboardingStatus.BLOCKED] in text, (
        f"the onboarding queue does not state "
        f"{ONBOARDING_STATUS_WORDS[OnboardingStatus.BLOCKED]!r}; its Situação cell is "
        f"either empty or showing `blocked`, which is the checklist column's own word"
    )


def test_the_gov_br_words_cover_every_tier_the_column_can_hold(
    portfolio: Portfolio,
) -> None:
    """The derived exclusion is only sound while every tier has an approved word.

    `GOVBR_ENGLISH_TOKENS` decides what to refuse by comparing each stored value against
    the word beside it, so a tier with no word falls into the conservative arm and is
    refused -- correct, but silent. This names it, and it also holds the fixture to
    spreading all four tiers across the four clients: two clients sharing a tier would
    leave one arm of the identity card rendered by nothing.
    """
    assert set(GOVBR_TRUST_WORDS) == set(GovBrTrustLevel), (
        f"GovBrTrustLevel defines {sorted(GovBrTrustLevel.values)} and this module "
        f"knows a word for {sorted(GOVBR_TRUST_WORDS)}; an unmapped tier is refused by "
        f"the conservative arm of GOVBR_ENGLISH_TOKENS rather than named"
    )
    assert set(GOVBR_TIER_BY_CLIENT.values()) == set(GovBrTrustLevel), (
        f"the fixture spreads {sorted(set(GOVBR_TIER_BY_CLIENT.values()))} over its "
        f"four clients, so a tier nobody holds is asserted on no rendered page"
    )
    seeded = {
        str(getattr(portfolio, str(status)).govbr_trust_level)
        for status in GOVBR_TIER_BY_CLIENT
    }
    assert seeded == {str(member.value) for member in GovBrTrustLevel}, (
        f"the seeded clients hold {sorted(seeded)}, so the fixture and the map above "
        f"disagree and a tier is asserted against a client that does not carry it"
    )
    assert sorted(GOVBR_ENGLISH_TOKENS) == [str(GovBrTrustLevel.UNKNOWN.value)], (
        f"the derivation refuses {sorted(GOVBR_ENGLISH_TOKENS)}; today exactly one "
        f"tier is stored under an English word, and a change to that set is a change "
        f"to what every firm page is scanned for"
    )


@pytest.mark.parametrize("status", sorted(CLIENT_STATUS_WORDS))
def test_the_workspace_identity_card_names_the_status_and_tier_in_portuguese(
    portfolio: Portfolio,
    status: str,
) -> None:
    """One client per status, one tier each, and the card read on its own page.

    This is the surface the closed plan recorded as an omission. `_identity.html`
    reached for `get_status_display` for the client's status and for its gov.br tier,
    and the labels behind both are the stored English words -- `active`, `suspended`,
    `closed` and `unknown` -- in front of the accountant who opened the client.
    """
    text, leaked = _scan_surface(portfolio, _workspace(str(status)))

    assert CLIENT_STATUS_WORDS[status] in text, (
        f"the identity card of the {status} client does not say "
        f"{CLIENT_STATUS_WORDS[status]!r}, so its SITUAÇÃO badge is empty or showing "
        f"the column's own word"
    )
    tier = GOVBR_TIER_BY_CLIENT[status]
    assert GOVBR_TRUST_WORDS[tier] in text, (
        f"the identity card does not say {GOVBR_TRUST_WORDS[tier]!r} for a client "
        f"whose gov.br column holds {tier!r}"
    )
    assert leaked == [], (
        f"the {status} client's workspace shows an accountant the stored word(s) "
        f"{leaked}"
    )


def test_the_workspace_obligation_table_states_all_five_words_in_portuguese(
    portfolio: Portfolio,
) -> None:
    """The one page where PAID and WAIVED reach a reader as rows rather than options.

    `apps/obligations/queries.py:53,104` excludes both from every queue, so
    `QUEUE_ROW_STATUSES` can only ever demand three of the five. The workspace's table
    filters on the client alone, which is what makes this the row-level proof that the
    map has all five arms and not merely the three a queue happens to reach.
    """
    text, _ = _scan_surface(portfolio, _workspace(str(ClientStatus.ACTIVE)))

    missing = [word for word in OBLIGATION_STATUS_WORDS.values() if word not in text]
    assert missing == [], (
        f"the workspace's obligation table is missing {missing}; one obligation per "
        f"status is seeded on this client and the table excludes none of them, so a "
        f"missing word is an arm that fell through rather than a row nobody has"
    )


def test_the_workspace_checklist_states_every_onboarding_word_in_portuguese(
    portfolio: Portfolio,
) -> None:
    """Four checklist items, four statuses, four badges -- read on one page.

    The onboarding QUEUE fetches blocked items only, so it reaches one arm of a four-arm
    chain. The workspace's checklist lists the client's items whatever their status,
    which is the surface where the other three are rendered by something.
    """
    text, _ = _scan_surface(portfolio, _workspace(str(ClientStatus.ACTIVE)))

    assert BLOCKED_REASON in text, (
        f"{BLOCKED_REASON!r} is absent, so the checklist this case reads its badges "
        f"from is not on the page"
    )
    missing = [word for word in ONBOARDING_STATUS_WORDS.values() if word not in text]
    assert missing == [], (
        f"the workspace checklist is missing {missing}; the fixture puts a different "
        f"status on each of its first four items, so a missing word is a badge that "
        f"fell through its chain"
    )


def test_the_obligation_words_are_the_ones_the_client_already_reads(
    portfolio: Portfolio,
) -> None:
    """A firm and its client must call the same row by the same name.

    The five words are pinned for the portal at
    `tests/portal/test_portal_payments.py:127-148`. Asserted here as a whole set rather
    than reused by import, because importing that module would drag its fixtures and its
    `pytestmark` in with it -- and because what has to agree is the WORDS, which a
    reword on either side would break silently while both files stayed green.
    """
    text, _ = _scan(portfolio, "queue-due-soon")

    assert set(OBLIGATION_STATUS_WORDS) == set(ObligationStatus), (
        f"ObligationStatus defines {sorted(ObligationStatus.values)} and this module "
        f"knows a word for {sorted(OBLIGATION_STATUS_WORDS)}; a status the enum grew "
        f"and the firm pages never named would render a cell nobody wrote copy for"
    )
    for status, word in OBLIGATION_STATUS_WORDS.items():
        assert word in text, (
            f"{status} is filtered as {word!r} on the portal and is missing from the "
            f"firm's own filter, so the two sides of this product name the same row "
            f"differently"
        )
