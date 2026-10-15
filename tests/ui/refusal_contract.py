"""What every R1 refusal page holds (AXR1-054), read from the HTTP response alone.

Shared by the view suites that own each refusal, so the cycle, export and account
tests assert one contract instead of three drifting copies.

The page is read as text, unescaped and stripped of markup, so a list repr cannot
hide behind autoescaping: `str(ValidationError(...))` rendered through the template
arrives as `[&#x27;...&#x27;]` and reads `['...']` here. That check runs before the
structural ones, so on a head that still answers `HttpResponse(str(exc))` the failure
names the repr rather than the missing `lang`.

Nothing here imports the code under test, and the template name is written out for
the same reason: the suites must collect on the parent head too, where these
assertions are expected to fail rather than the import.
"""

import html
import re
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # stubs-only: this type does not exist at runtime
    from django.test.client import _MonkeyPatchedWSGIResponse

REFUSAL_TEMPLATE: Final = "refusal/page.html"
LANG: Final = '<html lang="pt-br">'
TITLE: Final = re.compile(r"<title>(.*?)</title>", re.DOTALL)
H1: Final = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.DOTALL)
ERROR_STATE: Final = re.compile(
    r'<div\b[^>]*data-ui-state="error"[^>]*>(.*?)</div>', re.DOTALL
)
TAG: Final = re.compile(r"<[^>]+>")
LIST_REPRS: Final = ("['", '["')


def _text(fragment: str) -> str:
    return " ".join(html.unescape(TAG.sub(" ", fragment)).split())


def assert_readable_page(
    response: "_MonkeyPatchedWSGIResponse", *, status: int, reason: str
) -> str:
    """Assert the status, a plain-sentence reason and an accessible document.

    For a refusal drawn inside a screen's own page (an in-page error, a form
    error, a flash message) as much as for a refusal page. Returns the body.
    """
    body = response.content.decode()
    assert response.status_code == status, (response.status_code, body[:300])
    read = _text(body)
    for opener in LIST_REPRS:
        assert opener not in read, f"a Python list repr reached the reader: {read!r}"
    assert LANG in body, f"no document language in {body[:300]!r}"
    title = TITLE.search(body)
    assert title is not None, f"no document title in {body[:300]!r}"
    assert _text(title.group(1)), "the document title is empty"
    heading = H1.search(body)
    assert heading is not None, "the page carries no heading"
    assert _text(heading.group(1)), "the page heading is empty"
    assert reason in read, f"{reason!r} is not what the page says"
    return body


def assert_readable_refusal(
    response: "_MonkeyPatchedWSGIResponse", *, status: int, reason: str, back: str
) -> None:
    """Assert the status, the accessible page, the plain reason and the way back."""
    body = assert_readable_page(response, status=status, reason=reason)
    state = ERROR_STATE.search(body)
    assert state is not None, "the refusal is not drawn as the shared error state"
    said = _text(state.group(1))
    assert reason in said, f"{reason!r} is not what the refusal says: {said!r}"
    assert f'href="{back}"' in state.group(1), (
        f"the refusal offers no way back to {back}: {state.group(1)!r}"
    )
    rendered = [template.name for template in response.templates]
    assert REFUSAL_TEMPLATE in rendered, rendered
