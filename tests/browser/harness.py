"""What a browser test is handed: a signed-in page, a firm on real hosts, and axe.

Kept apart from `conftest.py` so the helpers are importable by name, and so the
conftest holds only the wiring that has to be fixtures.
"""

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from allauth.account.internal.flows.login import AUTHENTICATION_METHODS_SESSION_KEY
from allauth.mfa.models import Authenticator
from axe_playwright_python.sync_playwright import Axe
from django.conf import settings
from django.test import Client
from playwright.sync_api import Page, Response, ViewportSize

from apps.accounts.models import User
from apps.tenants.validators import PORTAL_HOST_SUFFIX
from tests.browser.middleware import OBSERVED, POLICED_ROLES
from tests.ui.factories import Firm

if TYPE_CHECKING:
    # The parameter type of `BrowserContext.add_cookies`, which the public
    # `playwright.sync_api` namespace does not re-export.
    from playwright._impl._api_structures import SetCookieParam

DESKTOP: Final = ViewportSize(width=1440, height=900)
MOBILE: Final = ViewportSize(width=390, height=844)
VIEWPORTS: Final[dict[str, ViewportSize]] = {"desktop": DESKTOP, "mobile": MOBILE}

# axe impact levels that fail a test. `moderate` and `minor` are reported by axe
# and read in review; they do not gate, because axe flags several of them on
# markup that is correct in context (a heading-order hint on a partial page).
BLOCKING_IMPACTS: Final = frozenset({"serious", "critical"})

# Where screenshots go when a run asks for them. Unset, nothing is written: a
# test that always wrote files would litter every developer's tree.
SCREENSHOT_DIR_ENV: Final = "BROWSER_SCREENSHOT_DIR"


class AxeViolationsError(AssertionError):
    """axe-core found a serious or critical accessibility violation."""


class PolicedRoleError(AssertionError):
    """A live request ran as a role the row-level-security policies do not bind."""


def axe_check(page: Page) -> list[dict[str, Any]]:
    """Run axe-core on the page as it stands and fail on serious or critical.

    Returns every violation axe reported, blocking or not, for a test that wants
    to say more. The raise names each rule, its impact, its help text and the
    first few offending selectors -- enough to find the element without a rerun.
    """
    response: dict[str, Any] = Axe().run(page).response
    violations: list[dict[str, Any]] = response["violations"]
    blocking = [v for v in violations if v.get("impact") in BLOCKING_IMPACTS]
    if blocking:
        lines = [
            f"{violation['id']} ({violation['impact']}): {violation['help']} -> "
            + ", ".join(str(node["target"]) for node in violation["nodes"][:5])
            for violation in blocking
        ]
        msg = (
            f"axe-core found {len(blocking)} serious/critical violation(s) on "
            f"{page.url}:\n  " + "\n  ".join(lines)
        )
        raise AxeViolationsError(msg)
    return violations


def assert_policed_roles() -> None:
    """Fail if any live request so far ran outside `POLICED_ROLES`, or none ran.

    Reads what `EffectiveRoleMiddleware` recorded. An empty record fails too:
    "no request ran as the wrong role" is vacuous when no request was checked.
    """
    observed = list(OBSERVED)
    if not observed:
        msg = (
            "no live request reached EffectiveRoleMiddleware; the role check did "
            "not run, so it proves nothing"
        )
        raise PolicedRoleError(msg)
    wrong = [(path, role) for path, role in observed if role not in POLICED_ROLES]
    if wrong:
        msg = (
            f"live requests ran as a role that bypasses row-level security: {wrong}; "
            f"expected one of {sorted(POLICED_ROLES)}"
        )
        raise PolicedRoleError(msg)


@dataclass
class LiveFirm:
    """A firm with its people, reachable on its own hosts of the live server."""

    firm: Firm
    port: int

    def url(self, path: str = "/", *, portal: bool = False) -> str:
        """Return an absolute URL on the firm host, or on its portal host."""
        slug = self.firm.tenant.slug
        host = f"{slug}{PORTAL_HOST_SUFFIX}" if portal else slug
        return f"http://{host}.localhost:{self.port}{path}"


def mfa_session_cookie(user: User, url: str) -> "SetCookieParam":
    """Sign `user` in by session, as though password and TOTP both succeeded.

    `force_login` writes the authenticated session; the authentication-method log
    allauth keeps is then filled in the shape its own login and MFA flows write,
    so a view that asks "was a second factor used, and when" gets the same answer
    as after a real sign-in. Typing a TOTP code in the browser is not an option:
    the window pin in `tests/support.py` patches the clock on the test thread, and
    the live server verifies on its own threads.
    """
    authenticator = Authenticator.objects.get(
        user=user,
        type=Authenticator.Type.TOTP,
    )
    client = Client()
    client.force_login(user)
    session = client.session
    signed_in_at = time.time()
    session[AUTHENTICATION_METHODS_SESSION_KEY] = [
        {"method": "password", "at": signed_in_at, "email": user.email},
        {"method": "mfa", "at": signed_in_at, "id": authenticator.pk, "type": "totp"},
    ]
    session.save()
    return {
        "name": settings.SESSION_COOKIE_NAME,
        "value": str(session.session_key),
        "url": url,
    }


@dataclass
class BrowserPage:
    """A Playwright page at one viewport, with its console watched."""

    page: Page
    viewport: str
    console_errors: list[str] = field(default_factory=list)

    def goto(self, url: str) -> Response:
        """Navigate, then fail at once if the request ran as the wrong role."""
        response = self.page.goto(url)
        assert_policed_roles()
        assert response is not None, f"no response navigating to {url}"
        assert response.ok, f"{url} answered {response.status}"
        return response

    def assert_clean_console(self) -> None:
        """No console error and no uncaught exception: CSP refusals land here."""
        assert self.console_errors == [], self.console_errors

    def screenshot(self, name: str) -> Path | None:
        """Save a full-page screenshot when `BROWSER_SCREENSHOT_DIR` is set."""
        directory = os.environ.get(SCREENSHOT_DIR_ENV)
        if not directory:
            return None
        path = Path(directory) / f"{name}-{self.viewport}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Hiding the caret leaves empty inline styles that HTMX history re-parses.
        self.page.screenshot(path=str(path), full_page=True, caret="initial")
        return path


__all__ = [
    "BLOCKING_IMPACTS",
    "DESKTOP",
    "MOBILE",
    "VIEWPORTS",
    "AxeViolationsError",
    "BrowserPage",
    "LiveFirm",
    "PolicedRoleError",
    "assert_policed_roles",
    "axe_check",
    "mfa_session_cookie",
]
