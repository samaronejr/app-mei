"""A refusal a signed-in firm reader navigated to, drawn as a page (AXR1-054).

These views used to answer a malformed filter, a stale form or a refused download
with a bare `HttpResponse(text, status=...)`: no document language, no title, no way
back, and in `cycle_open` a `ValidationError` printed with `str()`, so the reader saw
its list repr, brackets and quotes included.

Every caller has already passed its own tenant, capability and portfolio checks, so
the refusal is drawn inside the product shell like any other screen the reader may
open, and the failure itself is the shared `partials/ui/state_error.html` with its
plain-GET recovery link. Nothing is read to draw it: the heading and the reason are
the view's own sentences, and the way back is a route the same decorators admit.

`sentences` is the one way an exception becomes reader-facing text, for these pages
and for the in-page errors and form errors that used to print `str(exc)` too.

`forbidden_page` and `csrf_failure` draw the other refusal layout, the host-neutral
one under `templates/refusal/` (AXR1-059). A 403 can be answered on the firm, portal
or admin host to an account the shell no longer serves, so that page reads nothing
but whether someone is signed in, and leads back to the start or out through Sair.
"""

from http import HTTPStatus
from typing import Final, NamedTuple

from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

REFUSAL_TEMPLATE: Final = "refusal/page.html"
FORBIDDEN_TEMPLATE: Final = "403.html"
EXPIRED_TEMPLATE: Final = "refusal/page_expired.html"


class WayBack(NamedTuple):
    """Where the recovery link leads, and the words on it."""

    url: str
    label: str


def sentences(error: Exception) -> str:
    """Return what a refusal says: a ValidationError's messages, else its message."""
    if isinstance(error, ValidationError):
        return " ".join(error.messages)
    return str(error)


def refusal_page(
    request: HttpRequest, status: int, *, heading: str, reason: str, back: WayBack
) -> HttpResponse:
    """Render one refusal as a page, keeping the caller's status code."""
    return render(
        request,
        REFUSAL_TEMPLATE,
        {"heading": heading, "reason": reason, "back": back},
        status=status,
    )


def forbidden_page(request: HttpRequest, reason: str) -> HttpResponse:
    """Answer 403 with the caller's own sentence on the shared refusal layout.

    For a refusal about the state of the account or of a file, not about who may
    open the page, so the page leaves out its line on whom to ask for access.
    """
    return render(
        request,
        FORBIDDEN_TEMPLATE,
        {"reason": reason, "reason_only": True},
        status=HTTPStatus.FORBIDDEN,
    )


def csrf_failure(request: HttpRequest, reason: str = "") -> HttpResponse:
    """Answer a refused CSRF check (`CSRF_FAILURE_VIEW`) with the expired page.

    `reason` is Django's English for a developer. CsrfViewMiddleware has already
    logged it to `django.security.csrf`, and the reader is never shown it.
    """
    del reason
    return render(request, EXPIRED_TEMPLATE, status=HTTPStatus.FORBIDDEN)
