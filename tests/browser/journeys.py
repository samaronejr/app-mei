"""Shared wiring for the R1 golden journeys (T25, S15 A/B/D/I/J/K/O/Q).

The journeys are built from the same service fixtures the service-level scenario
tests use, so a browser journey and its service twin start from one world. What is
added here is only what a browser needs on top: the firm on live hosts, a portal
session on the portal host, an SMTP peer for the worker steps, and one call that
records a step (axe on the page as it stands, then a screenshot named for the step).

Worker steps -- the outbox dispatch, the scan verdict, extraction -- run on the test
thread through the real services, exactly where Celery would run them. Everything a
person does goes through the page.
"""

import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from allauth.mfa.models import Authenticator
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.core.tenancy import tenant_context
from apps.delivery.models import MessageState, OutboundMessage
from apps.tenants.models import Tenant
from tests.browser.harness import BrowserPage, LiveFirm, axe_check, mfa_session_cookie
from tests.cycles.cycle_fixtures import CycleSetup
from tests.support import enrol_totp
from tests.ui.factories import Firm

OpenPage = Callable[[LiveFirm, User | None], BrowserPage]

# Firm-side flags the R1 shell renders behind; `briefing_r1` turns `/` into Hoje.
R1_FLAGS = ("cycles_r1", "briefing_r1")


def live_graph(graph: CycleSetup, port: int, *flags: str) -> LiveFirm:
    for actor in (graph.actor, graph.staff):
        if not Authenticator.objects.filter(
            user=actor, type=Authenticator.Type.TOTP
        ).exists():
            enrol_totp(actor)
    if flags:
        enable(graph.tenant, *flags)
    firm = Firm(
        tenant=graph.tenant,
        owner=graph.actor,
        accountant=graph.staff,
        clients=[graph.client],
    )
    return LiveFirm(firm=firm, port=port)


def enable(tenant: Tenant, *flags: str) -> None:
    tenant.features = sorted({*tenant.features, *flags})
    tenant.save(update_fields=["features"])


def open_portal(open_page: OpenPage, live: LiveFirm, user: User) -> BrowserPage:
    portal = open_page(live, user)
    portal.page.context.add_cookies(
        [mfa_session_cookie(user, live.url("/", portal=True))]
    )
    return portal


def record(page: BrowserPage, step: str) -> None:
    """One journey step: axe on the page as it stands, then its screenshot.

    Screenshots land in `BROWSER_SCREENSHOT_DIR` as `AXR1-025-<step>-<viewport>.png`
    when the run asks for them; axe runs either way and fails on serious/critical.
    """
    axe_check(page.page)
    page.screenshot(f"AXR1-025-{step}")


def closed_port() -> int:
    """A local port nothing listens on: connecting is refused (SMTP down)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def smtp_at(port: int) -> Iterator[None]:
    with override_settings(
        EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
        EMAIL_HOST="127.0.0.1",
        EMAIL_PORT=port,
        EMAIL_HOST_USER="",
        EMAIL_HOST_PASSWORD="",
        EMAIL_USE_TLS=False,
        EMAIL_USE_SSL=False,
        EMAIL_TIMEOUT=3,
    ):
        yield


def release_retries(tenant: Tenant) -> None:
    """Bring temporarily failed messages due now: the backoff is not under test."""
    with tenant_context(tenant.pk):
        OutboundMessage.objects.filter(state=MessageState.TEMPORARY).update(
            next_attempt_at=timezone.now()
        )
