"""S15 J in a real browser: one contact, two companies, an explicit company switch.

The portal opens on the lowest company the account holds a seat in, offers the other
in the header, and after "Trocar empresa" every surface belongs to the chosen company:
its name is the page heading, its requests are listed, the other company's request
is neither listed nor reachable (404), and the session key was rotated.

The support half of S15 J: a platform staff member asks for access, the owner
approves it on /configuracoes/suporte/, and every firm page the staff member opens,
including after an HTMX swap, carries the "Acesso de suporte ativo" banner until the
owner ends the grant; the next request is then refused and shows no banner.

Both viewports. No fixed sleep: every wait is an auto-waiting `expect` or a response.
"""

from collections.abc import Callable
from pathlib import Path

from django.conf import settings as django_settings
from playwright.sync_api import expect
from pytest_django.fixtures import SettingsWrapper
from pytest_django.live_server_helper import LiveServer

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.cycles.models import EvidenceRequest
from apps.tenants.models import Membership, TenantRole
from apps.tenants.support import request_support_grant
from apps.tenants.support_models import SupportGrant
from tests.browser.harness import BrowserPage, LiveFirm, assert_policed_roles
from tests.browser.journeys import R1_FLAGS, live_graph, open_portal, record
from tests.portal.request_fixtures import PortalSetup, portal_setup
from tests.support import enrol_totp
from tests.ui.factories import PASSWORD

OpenPage = Callable[[LiveFirm, User | None], BrowserPage]


def _request_of(setup: PortalSetup, company: ClientCompany) -> EvidenceRequest:
    return setup.request if company == setup.graph.client else setup.other_request


def _session(portal: BrowserPage) -> str:
    return next(
        cookie.get("value", "")
        for cookie in portal.page.context.cookies()
        if cookie.get("name") == django_settings.SESSION_COOKIE_NAME
        and "-portal." in cookie.get("domain", "")
    )


def _owner_approves(  # noqa: PLR0913 - one journey actor and its request
    owner: BrowserPage,
    live: LiveFirm,
    setup: PortalSetup,
    grant: SupportGrant,
    *,
    email: str,
    purpose: str,
) -> None:
    owner.goto(live.url("/configuracoes/suporte/"))
    request_row = owner.page.locator(f"[data-grant='{grant.pk}']")
    expect(request_row).to_contain_text(email)
    expect(request_row).to_contain_text(purpose)
    request_row.get_by_label("Tipo de acesso").select_option("read_only")
    request_row.get_by_label("Duração (horas, até 72)").fill("2")
    request_row.get_by_role(
        "checkbox", name=setup.graph.client.legal_name, exact=True
    ).check()
    with owner.page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and response.url.endswith(f"/configuracoes/suporte/{grant.pk}/aprovar/")
        )
    ) as approved:
        request_row.get_by_role("button", name="Autorizar acesso").click()
    assert approved.value.status == 302
    history = owner.page.locator("section[aria-labelledby='historico']")
    expect(history.locator(f"[data-grant='{grant.pk}']")).to_contain_text("Aprovado")
    record(owner, "j-04-owner-approves-support")


def _owner_ends(owner: BrowserPage, live: LiveFirm, grant: SupportGrant) -> None:
    history = owner.page.locator("section[aria-labelledby='historico']")
    owner.goto(live.url("/configuracoes/suporte/"))
    with owner.page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and response.url.endswith(f"/configuracoes/suporte/{grant.pk}/revogar/")
        )
    ):
        history.locator(f"[data-grant='{grant.pk}']").get_by_role(
            "button", name="Encerrar acesso"
        ).click()
    expect(history.locator(f"[data-grant='{grant.pk}']")).to_contain_text("Encerrado")
    record(owner, "j-07-owner-ends-support")


def test_journey_j_company_switch_rescopes_every_surface(
    browser_server: LiveServer,
    open_page: OpenPage,
    settings: SettingsWrapper,
    tmp_path: Path,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    setup = portal_setup("journey-j")
    Membership.objects.create(
        user=setup.portal_user,
        tenant=setup.graph.tenant,
        client=setup.other_client,
        role=TenantRole.CLIENT_OWNER,
    )
    live = live_graph(setup.graph, browser_server.thread.port)
    first, second = sorted(
        (setup.graph.client, setup.other_client), key=lambda company: str(company.pk)
    )
    portal = open_portal(open_page, live, setup.portal_user)
    page = portal.page
    portal.goto(live.url("/", portal=True))
    expect(page.locator("h1")).to_have_text(first.legal_name)
    expect(
        page.locator(f"[data-request-id='{_request_of(setup, first).pk}']")
    ).to_be_visible()
    expect(
        page.locator(f"[data-request-id='{_request_of(setup, second).pk}']")
    ).to_have_count(0)
    switcher = page.get_by_label("Empresa", exact=True)
    expect(switcher).to_have_value(str(first.pk))
    record(portal, "j-01-first-company")

    before = _session(portal)
    switcher.select_option(str(second.pk))
    with page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and response.url.endswith("/empresa/selecionar/")
        )
    ) as switched:
        page.get_by_role("button", name="Trocar empresa").click()
    assert switched.value.status == 302
    expect(page.locator("h1")).to_have_text(second.legal_name)
    expect(switcher).to_have_value(str(second.pk))
    expect(
        page.locator(f"[data-request-id='{_request_of(setup, second).pk}']")
    ).to_be_visible()
    expect(
        page.locator(f"[data-request-id='{_request_of(setup, first).pk}']")
    ).to_have_count(0)
    assert _session(portal) != before
    record(portal, "j-02-second-company")

    foreign = page.goto(
        live.url(f"/solicitacoes/{_request_of(setup, first).pk}/", portal=True)
    )
    assert foreign is not None
    assert foreign.status == 404
    own = _request_of(setup, second)
    portal.goto(live.url(f"/solicitacoes/{own.pk}/", portal=True))
    expect(page.locator("h1")).to_have_text(own.title)
    record(portal, "j-03-second-company-request")
    assert_policed_roles()
    assert portal.console_errors == [
        "Failed to load resource: the server responded with a status of 404 (Not Found)"
    ], portal.console_errors


def test_journey_j_support_banner_follows_the_owner_approved_grant(
    browser_server: LiveServer,
    open_page: OpenPage,
    settings: SettingsWrapper,
    tmp_path: Path,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    setup = portal_setup("journey-j-support")
    live = live_graph(
        setup.graph, browser_server.thread.port, *R1_FLAGS, "firm_settings_r1"
    )
    support_user = User.objects.create_user(
        email="suporte@plataforma.example.com", password=PASSWORD, is_staff=True
    )
    enrol_totp(support_user)
    purpose = "Conferir o lembrete que o cliente não recebeu"
    with tenant_context(setup.graph.tenant.pk):
        grant = request_support_grant(
            support_user, setup.graph.tenant.pk, purpose=purpose
        )

    owner = open_page(live, setup.graph.actor)
    _owner_approves(
        owner, live, setup, grant, email=support_user.email, purpose=purpose
    )

    support = open_page(live, support_user)
    page = support.page
    banner = page.locator("[data-support-banner]")
    period = f"{setup.cycle.period_start:%Y-%m}"
    support.goto(live.url(f"/?escopo=team&periodo={period}"))
    expect(banner).to_be_visible()
    expect(banner).to_contain_text("Acesso de suporte ativo")
    expect(banner).to_contain_text("somente leitura")
    expect(banner).to_contain_text("Encerra automaticamente às")
    record(support, "j-05-support-banner-hoje")

    page.locator("#briefing-filters").click()
    with page.expect_response(
        lambda response: (
            response.status == 200
            and response.request.headers.get("hx-request") == "true"
        )
    ):
        page.get_by_role("button", name="Aplicar filtros").click()
    expect(page.locator("#briefing-queue")).to_be_focused()
    expect(banner).to_be_visible()

    support.goto(live.url(f"/clientes/{setup.graph.client.pk}/?periodo={period}"))
    expect(page.locator("h1")).to_contain_text(setup.graph.client.legal_name)
    expect(banner).to_be_visible()
    record(support, "j-06-support-banner-client")

    _owner_ends(owner, live, grant)

    ended = page.goto(live.url(f"/?escopo=team&periodo={period}"))
    assert ended is not None
    assert ended.status == 403
    expect(banner).to_have_count(0)
    record(support, "j-08-support-ended-refused")
    owner.assert_clean_console()
    assert all("status of 403" in error for error in support.console_errors), (
        support.console_errors
    )
    assert_policed_roles()
