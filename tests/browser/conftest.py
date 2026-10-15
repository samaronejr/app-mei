"""Wiring for the browser suite: a live server that runs as the production roles.

The suite is deselected from a plain `uv run pytest` (`addopts` carries
`-m "not browser"`) and runs as `uv run pytest -m browser`, after
`uv run playwright install chromium`.

THE ONE THING THAT MUST NOT GO WRONG HERE is the database role of the live
server. `tests/conftest.py` issues `SET ROLE app_runtime` on the TEST thread's
connection only. The live server answers on its own threads, each opening its own
connection as the login role `app_test` -- the table owner, holding BYPASSRLS
(`ops/sql/roles.sql`). Left alone, every page a browser test renders would read
every tenant's rows and look perfectly normal. Two pieces close that:

  * `_runtime_role_off_main_thread`, a `connection_created` receiver that issues
    `SET ROLE app_runtime` on every connection opened off the main thread;
  * `EffectiveRoleMiddleware` (tests/browser/middleware.py), installed for these
    tests only, which asks PostgreSQL `current_user` on every live request and
    refuses anything but app_runtime or app_portal. Every navigation through
    `BrowserPage.goto` then fails on the spot, naming the role. It is what makes
    the receiver's absence visible rather than silent.

Everything else is ordinary: a firm built by `tests/ui/factories.py` on
`<slug>.localhost` hosts, signed in by session injection with the MFA state
allauth writes, a Chromium context per viewport, and axe-core.

Waiting is done with Playwright's own auto-waiting `expect(...)` and
`expect_response`; there is no fixed sleep anywhere in this suite.
"""

import os
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Final

import pytest
from django.conf import settings
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.backends.signals import connection_created
from django.test.utils import override_settings
from playwright.sync_api import Browser, BrowserContext, ConsoleMessage, Error
from pytest_django.live_server_helper import LiveServer

from apps.accounts.models import User
from apps.core.flags import FEATURES
from tests.browser.harness import (
    VIEWPORTS,
    BrowserPage,
    LiveFirm,
    mfa_session_cookie,
)
from tests.browser.middleware import MIDDLEWARE_PATH, OBSERVED
from tests.ui.factories import make_firm

BROWSER_DIR: Final = Path(__file__).resolve().parent
RUNTIME_ROLE: Final = "app_runtime"
RECEIVER_UID: Final = "tests.browser.runtime-role-off-main-thread"
PRODUCT_SHELL_FLAG: Final = "cycles_r1"


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark everything under tests/browser `browser`, before `-m` deselects.

    `tryfirst` matters: marker deselection runs in pytest's own implementation of
    this hook, so a marker added after it would let a browser test into the
    default run and make `uv run pytest` require a Chromium it does not have.
    """
    for item in items:
        if item.path.is_relative_to(BROWSER_DIR):
            item.add_marker(pytest.mark.browser)


def _runtime_role_off_main_thread(
    sender: type[BaseDatabaseWrapper],
    connection: BaseDatabaseWrapper,
    **kwargs: object,
) -> None:
    """Put every live-server connection under the role production runs as.

    The main thread is left to `tests/conftest.py`, which switches it per test and
    must reset it before the flush -- `app_runtime` cannot TRUNCATE `app_test`'s
    tables.
    """
    if threading.current_thread() is threading.main_thread():
        return
    with connection.cursor() as cursor:
        cursor.execute(f"SET ROLE {RUNTIME_ROLE}")


@pytest.fixture(scope="session", autouse=True)
def _playwright_beside_the_orm() -> Iterator[None]:
    """Let the Django ORM run on the thread Playwright's event loop lives on.

    The sync Playwright API keeps an asyncio loop registered as running on the
    main thread for as long as the session-scoped browser lives, and Django's
    `async_unsafe` guard reads that as "called from async code" and refuses
    every query. Nothing here is async: the loop only relays browser commands.
    Session-scoped because the loop is, and set before the first query.
    """
    previous = os.environ.get("DJANGO_ALLOW_ASYNC_UNSAFE")
    os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("DJANGO_ALLOW_ASYNC_UNSAFE", None)
        else:
            os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = previous


@pytest.fixture(scope="package")
def browser_server() -> Iterator[LiveServer]:
    """A live server for this package, built after its settings are in place.

    pytest-django's `live_server` is session-scoped and loads the middleware
    chain the moment it starts, which may be before any fixture here runs. This
    one starts only once the role check, the test URLconf and the `*.localhost`
    hosts are installed, and it is torn down with the package, so none of the
    three outlives the browser tests.
    """
    connection_created.connect(_runtime_role_off_main_thread, dispatch_uid=RECEIVER_UID)
    overrides = override_settings(
        MIDDLEWARE=[*settings.MIDDLEWARE, MIDDLEWARE_PATH],
        ROOT_URLCONF="tests.browser.urls",
        ALLOWED_HOSTS=["localhost", "127.0.0.1", ".localhost"],
    )
    overrides.enable()
    server = LiveServer("localhost")
    try:
        yield server
    finally:
        server.stop()
        overrides.disable()
        connection_created.disconnect(dispatch_uid=RECEIVER_UID)


@pytest.fixture(autouse=True)
def _role_record(transactional_db: None) -> Iterator[None]:
    """Start each test with an empty role record; `transaction=True` for all.

    The live server reads the database on its own connections, which see only
    committed rows, so every browser test is transactional.
    """
    OBSERVED.clear()
    yield
    OBSERVED.clear()


@pytest.fixture
def live_firm(
    browser_server: LiveServer,
) -> Callable[..., LiveFirm]:
    """Build a firm (owner + staff accountant, TOTP enrolled) on live hosts.

    `features` defaults to the product-shell flag; pass `()` for a flag-off firm.
    """

    def build(
        slug: str = "acme", *, features: tuple[str, ...] = (PRODUCT_SHELL_FLAG,)
    ) -> LiveFirm:
        unknown = set(features) - FEATURES
        assert not unknown, f"not feature flags: {sorted(unknown)}"
        firm = make_firm(slug)
        firm.tenant.features = list(features)
        firm.tenant.save(update_fields=["features"])
        return LiveFirm(firm=firm, port=browser_server.thread.port)

    return build


@pytest.fixture(params=sorted(VIEWPORTS))
def viewport(request: pytest.FixtureRequest) -> str:
    """Each browser test runs at 1440x900 and at 390x844."""
    return str(request.param)


@pytest.fixture
def open_page(
    browser: Browser,
    viewport: str,
) -> Iterator[Callable[[LiveFirm, User | None], BrowserPage]]:
    """Open a signed-in page for a user of a live firm, at this test's viewport."""
    contexts: list[BrowserContext] = []

    def open_for(live: LiveFirm, user: User | None = None) -> BrowserPage:
        account = user or live.firm.owner
        context = browser.new_context(
            viewport=VIEWPORTS[viewport],
            is_mobile=viewport == "mobile",
            has_touch=viewport == "mobile",
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
        )
        contexts.append(context)
        context.add_cookies([mfa_session_cookie(account, live.url("/"))])
        page = context.new_page()
        opened = BrowserPage(page=page, viewport=viewport)

        def on_console(message: ConsoleMessage) -> None:
            if message.type == "error":
                opened.console_errors.append(message.text)

        def on_page_error(error: Error) -> None:
            opened.console_errors.append(f"uncaught: {error}")

        page.on("console", on_console)
        page.on("pageerror", on_page_error)
        return opened

    yield open_for
    for context in contexts:
        context.close()
