"""The account page is nothing but links, so linking is the whole of what it promises.

Every other portal screen can be wrong about a value. This one can only be wrong about
an address, and an address is the one thing Django's template layer will let a page get
wrong QUIETLY. `{% url 'x' as var %}` on a name the portal tree does not carry emits
`href=""` and renders on, so a page built entirely out of captured tags would answer 200
with three dead controls and satisfy every status assertion ever written about it. The
template therefore uses the direct form throughout, and the case below reads the names
off its SOURCE rather than off a render -- because a name that fails to reverse raises
during rendering, which means a scan of the document could only ever see the links that
already worked. Reversing is done against `apps.portal.urls` explicitly: `reverse()`
with no urlconf resolves against `ROOT_URLCONF`, which mounts none of these names, so an
unqualified call would raise whether or not the portal routes exist.

The other half is what this page must NOT do. It reads no table at all -- the address it
shows is materialised by the middleware before the transaction opens -- so "zero
queries" here is a real claim rather than a budget with headroom. It is asserted against
the window between `SET LOCAL ROLE app_portal` and `COMMIT`, and paired in the same case
with the landing page, whose window must contain a read. Without that pairing an
assertion that "no statement in this slice reads a relation" would hold just as firmly
against a detector that had stopped recognising reads, or a slice that never opened.

Both client roles are exercised. The gate is sign-in alone, deliberately -- `core.E010`
would refuse any capability this view could name, because none is FULL for both client
roles -- so the claim under test is that neither role is turned away, and it is asserted
for each separately rather than for a representative one.
"""

import re
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Final

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import NoReverseMatch, reverse
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.support import enrol_totp

pytestmark = pytest.mark.django_db(transaction=True)

PORTAL_HOST: Final = "acme-portal.localhost"
PORTAL_URLCONF: Final = "apps.portal.urls"
PASSWORD: Final = "sufficiently-long-passphrase"  # noqa: S105

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
ACCOUNT_TEMPLATE: Final[Path] = (
    PROJECT_ROOT / "apps" / "portal" / "templates" / "portal" / "account.html"
)

# `{% url 'name' %}`, either quote style. Mirrored from `test_portal_layout.py` rather
# than imported, as every guard in this package is: importing would drag that module's
# fixtures and its own `pytestmark` in with it.
URL_TAG: Final = re.compile(r"""\{%\s*url\s+(["'])(?P<name>[\w-]+)\1""")

# The three destinations this page exists to offer. Named here so that a template which
# quietly LOST one still reds: the reversal loop below is satisfied by any set of
# resolvable names at all, including the empty tail of a page that dropped two of them.
EXPECTED_DESTINATIONS: Final = frozenset(
    {
        "account_change_password",
        "mfa_index",
        "account_logout",
    },
)

ROLE_SWITCH: Final = "SET LOCAL ROLE app_portal"
COMMIT: Final = "COMMIT"

# A statement that reads a relation. Django quotes every table name it emits, so
# `FROM "obligations_obligation"` matches and the middleware's own `SELECT current_user`
# -- which has no FROM clause at all -- does not. That distinction is the point: the
# window always carries the role switch and the two GUC assignments, and a rule that
# counted statements rather than reads would be measuring the middleware.
RELATION_READ: Final = re.compile(r'\bFROM\s+"', re.IGNORECASE)

# The labels the page draws beside each destination. Meaning is carried by the pt-BR
# word, never by the chevron next to it.
CHANGE_PASSWORD_LABEL: Final = "Alterar senha"  # noqa: S105 - a link label, not a secret
MFA_LABEL: Final = "Autenticação de dois fatores"
LOGOUT_LABEL: Final = "Sair"

CSRF_FIELD: Final = 'name="csrfmiddlewaretoken"'


@pytest.fixture(autouse=True)
def _portal_urls(settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]
    # Pinned to the shipped tree: sibling modules in this package point the middleware
    # at `tests.portal.urls`, which mounts probes instead of pages.
    monkeypatch.setattr("apps.portal.middleware.PORTAL_URLCONF", PORTAL_URLCONF)


@dataclass(frozen=True)
class Firm:
    """One firm, one MEI client, and the two client roles that reach this page.

    No sibling company and no seeded rows, because there is nothing here for a policy to
    confine -- this page reads no table. What both roles are held for is the gate: the
    view names no capability, and the claim is that neither role is refused.
    """

    tenant: Tenant
    client: ClientCompany
    owner: User
    collaborator: User

    def signed_in_as(self, user: User) -> Client:
        """Return a test client already holding `user`'s session."""
        http = Client()
        http.force_login(user)
        return http


@pytest.fixture
def firm() -> Firm:
    tenant = Tenant.objects.create(name="Acme", slug="acme")
    with tenant_context(tenant.id):
        # ALL_OBJECTS_OK: fixture seeding under an established tenant context.
        company = ClientCompany.all_objects.create(
            tenant=tenant,
            legal_name="CLIENTE ALPHA",
            cnpj="11222333000181",
            is_mei=True,
        )

    owner = User.objects.create_user(email="dono@mei.example", password=PASSWORD)
    helper = User.objects.create_user(email="ajuda@mei.example", password=PASSWORD)
    # Anyone holding an active Membership is diverted to enrolment before reaching any
    # view, so an unenrolled account would measure the redirect rather than the page.
    for account in (owner, helper):
        enrol_totp(account)

    Membership.objects.create(
        user=owner,
        tenant=tenant,
        role=TenantRole.CLIENT_OWNER,
        client=company,
    )
    Membership.objects.create(
        user=helper,
        tenant=tenant,
        role=TenantRole.CLIENT_COLLABORATOR,
        client=company,
    )
    return Firm(tenant, company, owner, helper)


def _path(name: str = "portal-account") -> str:
    """Reverse a portal destination against the tree the portal host actually serves.

    Reversing rather than hard-coding `/conta/` also means a route registered a SECOND
    time -- which shadows silently rather than erroring -- is exercised through the
    pattern `reverse()` resolves to, instead of the path a test author assumed.
    """
    return str(reverse(name, urlconf=PORTAL_URLCONF))


def _account(http: Client) -> str:
    """GET the account page and return the rendered document, refusing anything else.

    The status gate is the non-vacuity control for every scan below it. A portal
    template reaching a table `app_portal` cannot read raises rather than returning a
    document, and a redirect returns an empty body that satisfies every "…is absent"
    assertion perfectly.
    """
    response = http.get(_path(), headers={"host": PORTAL_HOST})
    assert response.status_code == HTTPStatus.OK, (
        f"the account page answered {response.status_code}, so nothing scanned below "
        f"is a rendered page"
    )
    return str(response.content.decode())


def _relation_reads(http: Client, name: str) -> list[str]:
    """Return the statements reading a relation between the role switch and the commit.

    Mirrors `_portal_window` in `test_portal_payments.py` and `_page_statements` in
    `test_portal_home.py`, and carries their gates for their reasons: a slice taken from
    a request that never entered portal context would be empty, and an empty slice
    satisfies "this page reads nothing" perfectly.
    """
    with CaptureQueriesContext(connection) as captured:
        response = http.get(_path(name), headers={"host": PORTAL_HOST})
    assert response.status_code == HTTPStatus.OK, response.status_code
    statements = [row["sql"] for row in captured.captured_queries]

    role_at = next(
        (index for index, sql in enumerate(statements) if ROLE_SWITCH in sql),
        None,
    )
    assert role_at is not None, (
        f"no {ROLE_SWITCH!r} was issued for {name}, so the measured window never "
        f"opened and every assertion over it is vacuous; the statements were "
        f"{statements}"
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
        f"the portal transaction never committed for {name}: {statements}"
    )

    inside = statements[role_at + 1 : commit_at]
    assert inside, (
        f"{name} issued nothing at all between the role switch and {COMMIT}, so the "
        f"window is empty and reads cannot be distinguished from their absence"
    )
    return [sql for sql in inside if RELATION_READ.search(sql)]


# ------------------------------------------------------------------ (a) both roles


@pytest.mark.parametrize("role", ["owner", "collaborator"])
def test_the_account_page_answers_for_either_client_role(
    firm: Firm,
    role: str,
) -> None:
    """Sign-in is the whole gate, so neither client role may be turned away.

    `core.E010` requires any capability a portal view names to be FULL for BOTH client
    roles, and it reaches that check only for GATED callbacks -- an ungated one is
    skipped entirely. Leaving this view ungated is therefore a decision the checker
    cannot make for us, and this case is what holds it: a `require_can` added later to
    "tighten" the page would 403 whichever role its capability is below FULL for, and
    that role reds here.
    """
    account = getattr(firm, role)
    body = _account(firm.signed_in_as(account))

    assert account.email in body, (
        f"the page does not name the account it is signed in as, so a client cannot "
        f"tell which sign-in the three controls below would change ({role})"
    )
    for label in (CHANGE_PASSWORD_LABEL, MFA_LABEL, LOGOUT_LABEL):
        assert label in body, (
            f"{label!r} is missing from the account page as {role}; the meaning of "
            f"every row here is carried by its pt-BR word, never by its chevron"
        )


# ------------------------------------------------- (b) every address actually exists


def test_every_destination_the_page_names_reverses_on_the_portal_tree() -> None:
    """A name this tree lacks is a dead control on a page that still answers 200.

    Read off the template SOURCE rather than the rendered document, and that is the
    whole method. `{% url %}` in its direct form raises during rendering, so a name that
    fails to reverse never reaches a document at all -- a scan of the render could only
    ever enumerate the links that already worked, and would report a clean sheet for a
    page whose every anchor was broken.
    """
    assert ACCOUNT_TEMPLATE.is_file(), (
        f"{ACCOUNT_TEMPLATE} is not a shipped template, so the scan below reads nothing"
    )
    written = [
        match.group("name")
        for match in URL_TAG.finditer(ACCOUNT_TEMPLATE.read_text(encoding="utf-8"))
    ]
    named = list(dict.fromkeys(written))

    # The gate. A page that names no destination through the url tag -- because it was
    # rewritten with captured variables, or hollowed out entirely -- would send the loop
    # below over an empty list and pass without reversing anything.
    assert named, (
        f"{ACCOUNT_TEMPLATE.name} names no destination through the url tag, so the "
        f"reversal below quantifies over nothing and this case passes for a dead page"
    )
    # The second gate. The loop is satisfied by ANY set of resolvable names, so a page
    # that silently dropped two of its three controls would still be reported clean.
    missing = sorted(EXPECTED_DESTINATIONS - set(named))
    assert not missing, (
        f"{ACCOUNT_TEMPLATE.name} no longer names {missing}; this page exists to offer "
        f"exactly these three destinations"
    )

    offenders: list[str] = []
    for name in named:
        try:
            reverse(name, urlconf=PORTAL_URLCONF)
        except NoReverseMatch:
            offenders.append(name)

    assert not offenders, (
        f"{ACCOUNT_TEMPLATE.name} links {offenders}, which {PORTAL_URLCONF} does not "
        f"carry. `{{% url %}}` raises at render time for a name this tree lacks, so "
        f"each of these turns the whole account page into a 500 -- and a firm-side "
        f"name that reverses on ROOT_URLCONF looks perfectly correct until it is "
        f"reversed against the tree the portal host actually serves"
    )


# ------------------------------------------------------------- (c) it reads nothing


def test_the_page_reads_no_relation_between_the_role_switch_and_the_commit(
    firm: Firm,
) -> None:
    """Zero, not a budget with headroom: this page has no queryset to confine.

    Paired with the landing page in the same case on purpose. "No statement in this
    slice reads a relation" is satisfied just as firmly by a detector that had stopped
    recognising reads as by a page that issues none, and the control below is what tells
    the two apart. `portal-home` aggregates over `obligations_obligation`, so its window
    must contain a read; if it does not, nothing this case says about the account page
    means anything.
    """
    http = firm.signed_in_as(firm.owner)

    # The control: the detector can see a read when there is one to see.
    assert _relation_reads(http, "portal-home"), (
        "the landing page's window contains no relation read, so RELATION_READ is not "
        "recognising the reads it exists to find and the assertion below is vacuous"
    )

    reads = _relation_reads(http, "portal-account")

    assert reads == [], (
        f"the account page read {len(reads)} relation(s) under {ROLE_SWITCH!r}:\n  "
        + "\n  ".join(reads)
        + "\nThis page renders three static addresses and one attribute the "
        "middleware materialised before the transaction opened. A read here is "
        "something the page grew -- most likely a template reaching through "
        "`request.user` past the three attributes that cost nothing"
    )


# ------------------------------------------------------------ (d) signing out is a POST


def test_signing_out_is_a_post_carrying_its_token_first(firm: Firm) -> None:
    """A GET that ends a session is a link, and links get followed by machines.

    A prefetching browser, a link scanner, a chat client generating a preview or one
    accidental swipe all follow anchors, and any of them would sign the client out
    without a person having asked. The token is asserted as the form's FIRST child
    rather than merely present, so a rewrite that keeps the field but moves it past a
    hidden input somebody else's middleware injects is still the shape reviewed here.
    """
    body = _account(firm.signed_in_as(firm.owner))
    logout = _path("account_logout")

    opened = re.search(
        rf'<form\b[^>]*action="{re.escape(logout)}"[^>]*>',
        body,
        re.IGNORECASE,
    )
    assert opened is not None, (
        f"no <form> posts to {logout}; without one every assertion below quantifies "
        f"over an absent element"
    )
    assert 'method="post"' in opened.group(0).lower(), (
        f"the sign-out form is not a POST: {opened.group(0)}"
    )

    tail = body[opened.end() :].lstrip()
    assert tail.startswith("<input"), (
        f"the sign-out form's first child is not an input: {tail[:120]!r}"
    )
    first_child = tail[: tail.index(">") + 1]
    assert CSRF_FIELD in first_child, (
        f"the sign-out form's first child is not the CSRF token: {first_child!r}"
    )

    assert not re.search(
        rf'<a\b[^>]*href="{re.escape(logout)}"',
        body,
        re.IGNORECASE,
    ), (
        f"the account page also offers {logout} as an anchor; a GET that ends a "
        f"session is followed by prefetchers and scanners on the client's behalf"
    )
