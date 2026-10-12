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
"""

from typing import Final, NamedTuple

from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

REFUSAL_TEMPLATE: Final = "refusal/page.html"


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
