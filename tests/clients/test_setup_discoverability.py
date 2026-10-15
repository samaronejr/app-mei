"""AXR1-047: after a flagged CSV import, setup is reachable by following links.

The happy path is a journey, not a URL. The owner imports a portfolio, opens the
import result, follows the ordinary client link into the `cycles_r1` workspace and
follows the workspace's own link to `/clientes/<id>/cadastro/`, where the service,
the tax profile, the contacts and the responsible people are configured.

**Every navigation below uses an href (or a form action) parsed out of the page
that was actually rendered.** A test that reversed the destination itself and went
straight there would pass against a product that offers no way in — which is
precisely the failure this todo closes: the legacy detail template carries the
`client-setup` link, the workspace that replaces it under `cycles_r1` does not, and
its one onboarding link pointed at `client-detail` + `#contatos`, i.e. back at
itself.

The refusals are asserted from both sides, because they are two different
mechanisms and a change that drops either one has to turn this file red:

* the account is offered no link it would be refused (the view's `may_set_up`),
* and the route refuses the typed URL anyway (`require_feature` + `require_can`
  + the portfolio load inside the view).
"""

from html.parser import HTMLParser
from http import HTTPStatus
from typing import Any, Final

import pytest
from django.urls import reverse

from apps.clients.models import ClientCompany, ImportBatch
from apps.clients.tasks import commit_import_task
from apps.core.tenancy import tenant_context
from tests.authz.test_support_grant import (
    _firm_host,
    _grant_for,
    _session_for,
    _staff,
)
from tests.clients.test_import_wizard import csv_upload
from tests.ui.factories import Firm, assign, client_base, make_cnpj, make_firm

pytestmark = pytest.mark.django_db(transaction=True)

# Both flags: `cycles_r1` is what swaps the legacy detail template for the client
# workspace, and `import_r1` is what the import wizard and the setup route live
# behind. The combination is the only one in which this journey exists at all.
FLAGS: Final[tuple[str, ...]] = ("cycles_r1", "import_r1")

# The setup page's own section anchors — the four things the acceptance names, read
# as DOM ids rather than as prose, so a copy edit cannot fail this and a dropped
# section cannot pass it.
SETUP_SECTIONS: Final[tuple[str, ...]] = (
    'id="servicos"',
    'id="perfil"',
    'id="contatos"',
    'id="responsaveis"',
)

# The workspace section this todo adds the entry point to.
SETUP_SECTION_ID: Final = "cadastro"


def _flagged_firm(
    slug: str,
    *,
    flags: tuple[str, ...] = FLAGS,
    clients: int = 1,
) -> Firm:
    """Build a firm whose cohort carries exactly `flags`."""
    firm = make_firm(slug, client_count=clients)
    firm.tenant.features = list(flags)
    firm.tenant.save(update_fields=["features"])
    return firm


class _Anchors(HTMLParser):
    """Collect every anchor's `(label, href)` and every form action on a page.

    `section` narrows a second, separate list to the anchors inside one
    `<section id="...">`. The page-wide list is what the discoverability and the
    denial assertions quantify over — a link anywhere on the page is a link the
    reader can click — while the scoped list is what pins the behaviour of one
    section, where a drawer or a filter elsewhere on the workspace could otherwise
    answer for it.
    """

    def __init__(self, section: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.section_links: list[tuple[str, str]] = []
        self.actions: list[str] = []
        self.saw_section = False
        self._section = section
        self._in_section = False
        self._href: str | None = None
        self._label: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Open an anchor, enter the named section, or record a form's action."""
        attributes = {key: (value or "") for key, value in attrs}
        if tag == "section" and self._section:
            self._in_section = attributes.get("id") == self._section
            self.saw_section = self.saw_section or self._in_section
        elif tag == "a":
            self._href = attributes.get("href", "")
            self._label = []
        elif tag == "form":
            self.actions.append(attributes.get("action", ""))

    def handle_data(self, data: str) -> None:
        """Accumulate the text of the anchor currently open, if any."""
        if self._href is not None:
            self._label.append(data)

    def handle_endtag(self, tag: str) -> None:
        """Close the open anchor, or leave the named section."""
        if tag == "section":
            self._in_section = False
        elif tag == "a" and self._href is not None:
            found = ("".join(self._label).strip(), self._href)
            self.links.append(found)
            if self._in_section:
                self.section_links.append(found)
            self._href = None
            self._label = []


def _scan(body: str, *, where: str, section: str = "") -> _Anchors:
    """Parse one rendered page, refusing to report about markup that is not there."""
    scanner = _Anchors(section)
    scanner.feed(body)
    scanner.close()
    assert scanner.links, (
        f"{where}: the page rendered no anchor at all, so anything said about the "
        f"links it offers would be a statement about nothing"
    )
    assert not section or scanner.saw_section, (
        f"{where}: no <section id={section!r}> rendered, so scoping the scan to it "
        f"proves nothing about the links inside it"
    )
    return scanner


def _destination(href: str) -> str:
    """Return the path an href leads to, without its query or its fragment."""
    return href.partition("?")[0].partition("#")[0]


def _leads_to(links: list[tuple[str, str]], *, to: str) -> list[str]:
    """Return the rendered hrefs whose destination is `to`."""
    return [href for _, href in links if _destination(href) == to]


def _follow(scanner: _Anchors, *, to: str, where: str) -> str:
    """Return the rendered href that leads to `to`, naming what was offered if not.

    The returned string is the one the template produced, and it is what the test
    then requests. Reversing `to` again at the call site would turn the journey
    into a direct-URL visit and stop proving anything about discoverability.
    """
    found = _leads_to(scanner.links, to=to)
    assert found, (
        f"{where}: nothing on this page leads to {to!r}. It renders "
        f"{sorted({href for _, href in scanner.links})} — so the only way on is to "
        f"type the URL, which is the failure this test exists to catch"
    )
    return found[0]


def _submit_to(scanner: _Anchors, *, to: str, where: str) -> str:
    """Return the rendered form action that posts to `to`."""
    found = [action for action in scanner.actions if action == to]
    assert found, (
        f"{where}: no form on this page posts to {to!r}; its actions are "
        f"{sorted(scanner.actions)}"
    )
    return found[0]


def test_the_owner_walks_from_the_import_result_into_the_setup_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Import one client, then reach its setup page using only rendered links.

    The witness for this todo. At the parent commit it fails on the import result:
    that page reports counts and offers no link to a single client it created, so
    the walk stops there — and the workspace it cannot reach has no setup link
    either. Both halves of the change are needed to make it pass.

    The commit itself is a POST (an import is not a navigation), so its action is
    read off the rendered form and the queued `TenantTask` is run in-process, the
    way `tests/clients/test_import_wizard.py` runs it.
    """
    firm = _flagged_firm("t47-jornada", clients=0)
    browser = firm.as_owner()
    upload = csv_upload(
        [(make_cnpj(client_base(1, "t47-jornada")), "Padaria Importada MEI", "SP")],
    )

    planned = browser.post(reverse("client-import"), {"file": upload})
    assert planned.status_code == HTTPStatus.FOUND
    preview = browser.get(planned["Location"])
    assert preview.status_code == HTTPStatus.OK

    with tenant_context(firm.tenant.id):
        batch = ImportBatch.objects.get()
    queued: list[tuple[Any, ...]] = []
    monkeypatch.setattr(commit_import_task, "delay", lambda *args: queued.append(args))
    action = _submit_to(
        _scan(preview.content.decode(), where="import preview"),
        to=reverse("client-import-commit", args=[batch.pk]),
        where="import preview",
    )
    committed = browser.post(action, {"version": batch.version})
    assert committed.status_code == HTTPStatus.FOUND
    assert queued, "the commit view queued no import task"
    commit_import_task.apply(args=list(queued[0])).get()
    with tenant_context(firm.tenant.id):
        company = ClientCompany.objects.get(import_batch=batch)

    result = browser.get(committed["Location"])
    assert result.status_code == HTTPStatus.OK
    client_href = _follow(
        _scan(result.content.decode(), where="import result"),
        to=reverse("client-detail", args=[company.pk]),
        where="import result",
    )

    workspace = browser.get(client_href)
    assert workspace.status_code == HTTPStatus.OK
    offered = _scan(workspace.content.decode(), where="client workspace")
    setup_href = _follow(
        offered,
        to=reverse("client-setup", args=[company.pk]),
        where="client workspace",
    )
    label = next(text for text, href in offered.links if href == setup_href)
    assert label, "the workspace's setup link renders with no text to read or click"

    setup = browser.get(setup_href)
    assert setup.status_code == HTTPStatus.OK
    body = setup.content.decode()
    missing = [section for section in SETUP_SECTIONS if section not in body]
    assert not missing, f"the setup page reached this way carries no {missing}"


def test_an_assigned_accountant_reaches_the_setup_page_from_the_workspace() -> None:
    """A staff accountant holding the assignment follows the same link and is served.

    `clients.manage` is FULL for `staff_accountant` in the published matrix, so the
    narrowing that decides this case is the assignment, not the capability: the
    workspace renders because the client is in their book, and the link is drawn
    because the route would answer them.
    """
    firm = _flagged_firm("t47-atribuido")
    company = firm.clients[0]
    assign(firm, company, firm.accountant)
    session = firm.as_accountant()

    workspace = session.get(reverse("client-detail", args=[company.pk]))
    assert workspace.status_code == HTTPStatus.OK
    href = _follow(
        _scan(workspace.content.decode(), where="assigned workspace"),
        to=reverse("client-setup", args=[company.pk]),
        where="assigned workspace",
    )
    assert session.get(href).status_code == HTTPStatus.OK


def test_a_read_only_support_session_is_offered_no_link_and_is_still_refused() -> None:
    """The read-only visitor sees the workspace and no way to change the cadastro.

    A support session is the firm-side read-only identity: `clients.view_all` is
    FULL for it, so the workspace renders, and every write capability —
    `clients.manage` included — resolves NONE, so the entry point must not be
    drawn and the route must refuse it. 403, not 404: the grant already admits
    this account to the firm and to this client.
    """
    firm = _flagged_firm("t47-suporte")
    company = firm.clients[0]
    staff = _staff()
    _grant_for(firm.tenant, firm.owner, staff, client_ids=(company.pk,))
    session = _session_for(staff, _firm_host(firm))
    setup_url = reverse("client-setup", args=[company.pk])

    workspace = session.get(reverse("client-detail", args=[company.pk]))
    assert workspace.status_code == HTTPStatus.OK
    scanner = _scan(workspace.content.decode(), where="support workspace")
    assert _leads_to(scanner.links, to=setup_url) == []

    assert session.get(setup_url).status_code == HTTPStatus.FORBIDDEN


def test_an_unassigned_accountant_and_another_firms_owner_are_answered_404() -> None:
    """Outside the portfolio, the workspace and the setup page are both absent.

    404 rather than 403 is the contract `load_client` and `visible_clients` keep:
    the URL carries the client id, and a refusal that distinguished "exists but
    not yours" would turn the address bar into an enumeration oracle.
    """
    firm = _flagged_firm("t47-escopo")
    other = _flagged_firm("t47-outro", clients=0)
    company = firm.clients[0]
    paths = (
        reverse("client-detail", args=[company.pk]),
        reverse("client-setup", args=[company.pk]),
    )

    unassigned = firm.as_accountant()
    foreign = other.as_owner()
    for session, who in ((unassigned, "unassigned accountant"), (foreign, "foreign")):
        for path in paths:
            response = session.get(path)
            assert response.status_code == HTTPStatus.NOT_FOUND, (who, path)
            assert company.legal_name not in response.content.decode()


def test_the_workspace_offers_no_setup_link_while_the_import_flag_is_off() -> None:
    """With `import_r1` off the setup route is absent, so nothing may point at it."""
    firm = _flagged_firm("t47-sem-import", flags=("cycles_r1",))
    company = firm.clients[0]
    browser = firm.as_owner()
    setup_url = reverse("client-setup", args=[company.pk])

    workspace = browser.get(reverse("client-detail", args=[company.pk]))
    assert workspace.status_code == HTTPStatus.OK
    scanner = _scan(workspace.content.decode(), where="flag-off workspace")
    assert _leads_to(scanner.links, to=setup_url) == []

    assert browser.get(setup_url).status_code == HTTPStatus.NOT_FOUND


def test_the_legacy_detail_page_keeps_its_own_way_into_the_setup_page() -> None:
    """With `cycles_r1` off, the legacy screen's own setup link still works.

    The pre-existing entry point, asserted the same way as the new one: it is the
    behaviour this todo must leave alone, and reversing the URL here instead of
    following the rendered link would not notice if it were dropped.
    """
    firm = _flagged_firm("t47-legado", flags=("import_r1",))
    company = firm.clients[0]
    browser = firm.as_owner()

    detail = browser.get(reverse("client-detail", args=[company.pk]))
    assert detail.status_code == HTTPStatus.OK
    href = _follow(
        _scan(detail.content.decode(), where="legacy detail"),
        to=reverse("client-setup", args=[company.pk]),
        where="legacy detail",
    )
    assert browser.get(href).status_code == HTTPStatus.OK


def test_the_cadastro_section_never_sends_the_reader_back_to_this_page() -> None:
    """The onboarding section's links leave the page they are on.

    The bug in words: under `cycles_r1`, `client-detail` IS the workspace, so a
    link to `client-detail#contatos` reloaded the workspace and scrolled to its own
    Contatos section. Scoped to the section and limited to self-links that carry a
    fragment, so the period filter and a URL-addressed drawer elsewhere on the page
    cannot answer for it.
    """
    firm = _flagged_firm("t47-loop")
    company = firm.clients[0]
    own_path = reverse("client-detail", args=[company.pk])

    workspace = firm.as_owner().get(f"{own_path}?periodo=2026-09")
    assert workspace.status_code == HTTPStatus.OK
    scanner = _scan(
        workspace.content.decode(),
        where="workspace cadastro section",
        section=SETUP_SECTION_ID,
    )

    loops = [
        href
        for _, href in scanner.section_links
        if _destination(href) == own_path and "#" in href
    ]
    assert loops == [], (
        f"the Cadastro section links back to this very page at {loops}: following "
        f"it reloads the workspace instead of reaching the setup page"
    )
