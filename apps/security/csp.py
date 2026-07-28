"""Emit the Content-Security-Policy header on every response.

The policy names no external origin at all, which is only possible because HTMX and
Alpine are vendored into `static/` rather than pulled from a CDN. That is the whole
point of vendoring them: a CSP whose `script-src` lists a third party is a CSP that
trusts whatever that third party serves tomorrow.

Two directives are load-bearing and neither may be relaxed:

* `script-src 'self'` with **no** `'unsafe-inline'` and **no** `'unsafe-eval'`. The
  Alpine distribution shipped here is `@alpinejs/csp`, whose evaluator is a restricted
  interpreter rather than `new Function`; the ordinary build would need `'unsafe-eval'`
  and would then happily execute whatever ended up inside an `x-*` attribute. HTMX
  keeps one `new Function` call for `hx-on:` handlers, so `allowEval` is turned off in
  the `htmx-config` meta tag and a test asserts no template uses `hx-on`.
* `style-src 'self'` with no `'unsafe-inline'`. HTMX would otherwise inject its
  indicator rules as an inline `<style>` element; `includeIndicatorStyles` is off and
  the rules live in the compiled stylesheet instead.

Registered immediately after `SecurityMiddleware`, so its response phase runs near the
end of the chain and therefore covers responses produced by *inner* middleware too —
the 429 from the rate limiter and the 403 from `require_can` included.
"""

from collections.abc import Callable
from typing import Final

from django.conf import settings
from django.http import HttpRequest
from django.http.response import HttpResponseBase

HEADER: Final = "Content-Security-Policy"

# Ordered so a reader meets the fallback first and the exceptions after it.
DEFAULT_POLICY: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("default-src", ("'self'",)),
    ("script-src", ("'self'",)),
    ("style-src", ("'self'",)),
    # data: is for inlined SVG icons only. It cannot carry script under this policy,
    # because `script-src` does not list it.
    ("img-src", ("'self'", "data:")),
    ("font-src", ("'self'",)),
    ("connect-src", ("'self'",)),
    ("form-action", ("'self'",)),
    ("frame-ancestors", ("'none'",)),
    ("frame-src", ("'none'",)),
    # Without this a single injected <base> element re-points every relative script
    # URL in the page at an attacker's host while `script-src 'self'` still passes.
    ("base-uri", ("'none'",)),
    ("object-src", ("'none'",)),
)

SCRIPT_DIRECTIVES: Final = frozenset({"script-src", "script-src-elem", "default-src"})
FORBIDDEN_IN_SCRIPTS: Final = frozenset({"'unsafe-inline'", "'unsafe-eval'"})


def build_policy(
    directives: tuple[tuple[str, tuple[str, ...]], ...] = DEFAULT_POLICY,
) -> str:
    """Render the directive table as a header value.

    Raises `ValueError` when a script directive would allow inline code or `eval`.
    That check lives in the builder rather than in a test so a deployment cannot
    quietly widen the policy through settings and stay green.
    """
    for name, sources in directives:
        if name not in SCRIPT_DIRECTIVES:
            continue
        offending = FORBIDDEN_IN_SCRIPTS.intersection(sources)
        if offending:
            msg = (
                f"{name} may not contain {sorted(offending)}: the vendored Alpine CSP "
                f"build and the htmx-config meta tag exist precisely so that neither "
                f"is needed."
            )
            raise ValueError(msg)
    return "; ".join(f"{name} {' '.join(sources)}" for name, sources in directives)


class ContentSecurityPolicyMiddleware:
    """Attach the policy to every response that does not already carry one."""

    def __init__(
        self,
        get_response: Callable[[HttpRequest], HttpResponseBase],
    ) -> None:
        """Store the next callable and render the policy once, at startup."""
        self.get_response = get_response
        self.policy = build_policy(
            getattr(settings, "CONTENT_SECURITY_POLICY", DEFAULT_POLICY),
        )

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        """Run the request and stamp the policy on whatever comes back."""
        response = self.get_response(request)
        response.setdefault(HEADER, self.policy)
        return response


__all__ = [
    "DEFAULT_POLICY",
    "HEADER",
    "ContentSecurityPolicyMiddleware",
    "build_policy",
]
