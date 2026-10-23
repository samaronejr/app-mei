"""What a browser test is handed: a signed-in page, a firm on real hosts, and axe.

Kept apart from `conftest.py` so the helpers are importable by name, and so the
conftest holds only the wiring that has to be fixtures.
"""

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, TypedDict
from urllib.parse import parse_qs, urlsplit

from allauth.account.internal.flows.login import AUTHENTICATION_METHODS_SESSION_KEY
from allauth.mfa.models import Authenticator
from axe_playwright_python.sync_playwright import Axe
from django.conf import settings
from django.test import Client
from playwright.sync_api import Error as PlaywrightError
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
ANDROID_USER_AGENT: Final = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 6a) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)
ANDROID_PROFILE: Final = {
    "download_mbps": 9,
    "upload_mbps": 9,
    "rtt_ms": 170,
    "cpu_slowdown": 4,
    "viewport": {"width": 390, "height": 844},
    "device_scale_factor": 2.625,
    "user_agent": ANDROID_USER_AGENT,
}
VIEWPORTS: Final[dict[str, ViewportSize]] = {"desktop": DESKTOP, "mobile": MOBILE}

# axe impact levels that fail a test. `moderate` and `minor` are reported by axe
# and read in review; they do not gate, because axe flags several of them on
# markup that is correct in context (a heading-order hint on a partial page).
BLOCKING_IMPACTS: Final = frozenset({"serious", "critical"})

# Where screenshots go when a run asks for them. Unset, nothing is written: a
# test that always wrote files would litter every developer's tree.
SCREENSHOT_DIR_ENV: Final = "BROWSER_SCREENSHOT_DIR"

# The one capture failure that is retried (T90/AXRM-003): Chromium refusing to hand
# back a frame for a page that has finished loading, recorded repeatedly in the
# header-fit matrix under gate load right after "fonts loaded". Nothing else is
# retried -- not an assertion, not a timeout, not any other protocol error.
CAPTURE_PROTOCOL_ERROR: Final = (
    "Protocol error (Page.captureScreenshot): Unable to capture screenshot"
)
# A stable element every rendered page has; the capture waits for it to be laid out.
CAPTURE_READY_SELECTOR: Final = "body"


class AxeViolationsError(AssertionError):
    """axe-core found a serious or critical accessibility violation."""


class PolicedRoleError(AssertionError):
    """A live request ran as a role the row-level-security policies do not bind."""


class SwapTiming(TypedDict):
    """Browser-clock latency ends at the actual populated replacement DOM."""

    elapsed_ms: float
    response_url: str
    populated_total: int


def throttle_android(page: Page) -> None:
    """Apply CDP transport and CPU throttling to an already mobile context."""
    session = page.context.new_cdp_session(page)
    session.send("Network.enable")
    session.send(
        "Network.emulateNetworkConditions",
        {
            "offline": False,
            "latency": 170,
            "downloadThroughput": 9_000_000 / 8,
            "uploadThroughput": 9_000_000 / 8,
            "connectionType": "cellular4g",
        },
    )
    session.send("Emulation.setCPUThrottlingRate", {"rate": 4})


def measure_briefing_swap(
    page: Page, period: str, *, absent_signal: bool = False
) -> tuple[SwapTiming, Response]:
    """Subscribe before clicking; require the matching response and replacement DOM.

    The failure control cancels only this real request's swap, never its HTTP
    response. Its bounded missing-signal error must not become a latency sample.
    """
    form = page.locator('form[hx-target="#briefing-content"]')
    form.locator('[name="periodo"]').fill(period)
    form.locator('[name="escopo"]').select_option("team")
    button = form.locator('button[type="submit"]')
    button.scroll_into_view_if_needed()
    page.evaluate(
        """({period, absentSignal}) => {
            const original = document.getElementById('briefing-content');
            const form = original.querySelector('form[hx-target="#briefing-content"]');
            const button = form.querySelector('button[type="submit"]');
            const expected = new URL(form.getAttribute('hx-get'), location.href);
            let started;
            let timer;
            const matches = event => {
                const xhr = event.detail.xhr;
                if (!xhr || !xhr.responseURL) return false;
                const url = new URL(xhr.responseURL);
                return url.origin === expected.origin &&
                    url.pathname === expected.pathname &&
                    url.searchParams.get('periodo') === period &&
                    url.searchParams.get('escopo') === 'team';
            };
            const cancel = event => {
                if (matches(event) && absentSignal) event.detail.shouldSwap = false;
            };
            window.__axr1PerfSwap = new Promise((resolve, reject) => {
                const cleanup = () => {
                    clearTimeout(timer);
                    document.removeEventListener('htmx:afterSwap', swapped);
                    document.removeEventListener('htmx:beforeSwap', cancel);
                    button.removeEventListener('click', clicked, true);
                };
                const swapped = event => {
                    if (!matches(event) || started === undefined) return;
                    const current = document.getElementById('briefing-content');
                    if (!current || current === original || original.isConnected ||
                        event.detail.target.id !== 'briefing-content') return;
                    if (current.querySelector('#briefing-error, #briefing-unavailable'))
                        return;
                    const selected = current.querySelector('[name="periodo"]');
                    const total = current.querySelector('[data-briefing-total]');
                    if (!selected || selected.value !== period || !total ||
                        Number(total.dataset.briefingTotal) <= 0) return;
                    const elapsed = performance.now() - started;
                    cleanup();
                    resolve({elapsed_ms: elapsed,
                        response_url: event.detail.xhr.responseURL,
                        populated_total: Number(total.dataset.briefingTotal)});
                };
                const clicked = () => {
                    started = performance.now();
                    timer = setTimeout(() => {
                        cleanup();
                        reject(new Error('AXR1 missing exact populated HTMX swap'));
                    }, 15000);
                };
                // Event subscriptions exist before Playwright triggers the click.
                document.addEventListener('htmx:afterSwap', swapped);
                document.addEventListener('htmx:beforeSwap', cancel);
                button.addEventListener('click', clicked, {once: true, capture: true});
            });
            // Avoid an unhandled rejection if expect_response fails first.
            window.__axr1PerfSwap.catch(() => {});
        }""",
        {"period": period, "absentSignal": absent_signal},
    )

    def matching_response(response: Response) -> bool:
        url = urlsplit(response.url)
        query = parse_qs(url.query)
        return (
            url.netloc == urlsplit(page.url).netloc
            and url.path == "/"
            and query.get("periodo") == [period]
            and query.get("escopo") == ["team"]
            and response.request.method == "GET"
            and response.request.headers.get("hx-request") == "true"
        )

    with page.expect_response(matching_response, timeout=15_000) as response_info:
        button.click(timeout=15_000)
    response = response_info.value
    assert response.status == 200, response.status
    timing: SwapTiming = page.evaluate("() => window.__axr1PerfSwap")
    return timing, response


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


def capture_retrying_protocol_error(
    capture: Callable[[], object], failure_artifact: Path, state: str
) -> None:
    """Run `capture`; on `CAPTURE_PROTOCOL_ERROR` only, keep the failure and retry once.

    The first failure is written to `failure_artifact` (error text plus `state`) so a
    passing run still shows that the retry was needed. Any other exception, and a
    second protocol error, propagates unchanged.
    """
    try:
        capture()
    except PlaywrightError as error:
        if CAPTURE_PROTOCOL_ERROR not in error.message:
            raise
        failure_artifact.write_text(
            f"at={datetime.now(tz=UTC).isoformat()}\n{state}\n{error.message}\n",
            encoding="utf-8",
        )
        capture()


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

    def screenshot(self, name: str, *, mid_request: bool = False) -> Path | None:
        """Save a full-page screenshot when `BROWSER_SCREENSHOT_DIR` is set.

        A step that captures a request deliberately held in flight (an upload under
        throttling, a held route) passes `mid_request=True`: `networkidle` cannot fire
        while that request is open, so only the caller's own state wait applies.
        """
        directory = os.environ.get(SCREENSHOT_DIR_ENV)
        if not directory:
            return None
        path = Path(directory) / f"{name}-{self.viewport}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Capture only once the page has settled and its body is laid out.
        if not mid_request:
            self.page.wait_for_load_state("networkidle")
        self.page.locator(CAPTURE_READY_SELECTOR).wait_for(state="visible")
        capture_retrying_protocol_error(
            # Hiding the caret leaves empty inline styles that HTMX history re-parses.
            lambda: self.page.screenshot(
                path=str(path), full_page=True, caret="initial"
            ),
            path.with_name(f"{path.stem}.capture-error.txt"),
            f"url={self.page.url} viewport={self.page.viewport_size}",
        )
        return path


__all__ = [
    "BLOCKING_IMPACTS",
    "CAPTURE_PROTOCOL_ERROR",
    "DESKTOP",
    "MOBILE",
    "VIEWPORTS",
    "AxeViolationsError",
    "BrowserPage",
    "LiveFirm",
    "PolicedRoleError",
    "assert_policed_roles",
    "axe_check",
    "capture_retrying_protocol_error",
    "mfa_session_cookie",
]
