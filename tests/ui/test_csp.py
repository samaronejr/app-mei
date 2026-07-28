"""The Content-Security-Policy, and the template facts that let it stay this strict.

Three of these cases are guards rather than behaviour tests. They exist because the
policy is only as strong as the templates underneath it: one `<script src>` pointing
at a CDN, one `hx-on:` handler, or one tenant-controlled value interpolated into an
`x-*` attribute would each force a relaxation that quietly re-opens the hole.
"""

from http import HTTPStatus

import pytest
from django.test import Client
from django.urls import reverse

from apps.security.csp import (
    DEFAULT_POLICY,
    HEADER,
    ContentSecurityPolicyMiddleware,
    build_policy,
)
from tests.ui.templates_scan import (
    EXTERNAL_URL,
    VARIABLE,
    attributes_of,
    loaded_templates,
)

# Attributes whose value is a URL the browser will fetch. A scheme or `//` in any of
# them is an external origin, whatever the CSP says.
URL_ATTRIBUTES = frozenset({"src", "href", "action", "data", "poster"})

# Attributes evaluated as code by Alpine or HTMX after the browser has already decoded
# the HTML entities Django's autoescaping produced.
SCRIPTED_PREFIXES = ("x-", "hx-", "@", ":")


def _directive(policy: str, name: str) -> list[str]:
    for chunk in policy.split(";"):
        parts = chunk.split()
        if parts and parts[0] == name:
            return parts[1:]
    pytest.fail(f"policy has no {name} directive: {policy!r}")


def test_policy_allows_no_external_script_origin() -> None:
    sources = _directive(build_policy(), "script-src")
    assert sources == ["'self'"]


def test_policy_never_allows_inline_or_eval_for_scripts() -> None:
    for directive in ("default-src", "script-src"):
        sources = _directive(build_policy(), directive)
        assert "'unsafe-inline'" not in sources
        assert "'unsafe-eval'" not in sources


def test_policy_denies_framing_and_base_uri_rewriting() -> None:
    assert _directive(build_policy(), "frame-ancestors") == ["'none'"]
    # Without this, one injected <base> re-points every relative script URL at an
    # attacker's host while `script-src 'self'` still reports a pass.
    assert _directive(build_policy(), "base-uri") == ["'none'"]


@pytest.mark.parametrize("unsafe", ["'unsafe-inline'", "'unsafe-eval'"])
def test_builder_refuses_a_widened_script_directive(unsafe: str) -> None:
    widened = (("script-src", ("'self'", unsafe)),)
    with pytest.raises(ValueError, match="script-src may not contain"):
        build_policy(widened)


@pytest.mark.django_db
def test_header_is_served_on_a_real_response() -> None:
    response = Client().get(reverse("dsr-submit"))
    assert response.status_code == HTTPStatus.OK
    assert response.headers[HEADER] == build_policy()


@pytest.mark.django_db
def test_header_survives_a_refusal_from_inner_middleware() -> None:
    """A 429 is produced by middleware registered inside this one, and still gets it.

    The response phase runs in reverse registration order, so this asserts the
    ordering in MIDDLEWARE rather than merely the header's existence.
    """
    client = Client()
    url = reverse("dsr-submit")
    statuses = {client.post(url, {}).status_code for _ in range(40)}
    assert HTTPStatus.TOO_MANY_REQUESTS in statuses, "rate limiter never engaged"
    refused = client.post(url, {})
    assert refused.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert refused.headers[HEADER] == build_policy()


def test_middleware_renders_the_configured_policy_once() -> None:
    middleware = ContentSecurityPolicyMiddleware(lambda request: None)  # type: ignore[arg-type, return-value]
    assert middleware.policy == build_policy(DEFAULT_POLICY)


def test_no_template_loads_an_external_origin() -> None:
    offenders = [
        f"{path}: {name}={value!r}"
        for path, text in loaded_templates()
        for name, value in attributes_of(text)
        if name in URL_ATTRIBUTES and EXTERNAL_URL.match(value)
    ]
    assert offenders == [], (
        "a template names an external origin; vendor the asset under static/ instead"
    )


def test_no_template_mentions_a_cdn_host() -> None:
    offenders = [
        str(path) for path, text in loaded_templates() if "cdn." in text.lower()
    ]
    assert offenders == []


def test_no_template_uses_an_hx_on_handler() -> None:
    """`hx-on:` is the one HTMX feature that compiles a string with `new Function`.

    `allowEval: false` in the htmx-config meta disables it, so a handler added later
    would silently stop working. Failing here says why.
    """
    offenders = [
        f"{path}: {name}"
        for path, text in loaded_templates()
        for name, _ in attributes_of(text)
        if name.startswith("hx-on")
    ]
    assert offenders == []


def test_no_tenant_value_is_interpolated_into_a_scripted_attribute() -> None:
    """Autoescaping does not protect an attribute Alpine or HTMX later evaluates.

    Django renders `'` as `&#x27;`, but the browser decodes attribute values *before*
    Alpine reads them — so a client's `legal_name` interpolated into `x-data` escapes
    into the expression. Template tags are server-authored and allowed; `{{ }}` is a
    tenant-controlled value and is not.
    """
    offenders = [
        f"{path}: {name}={value!r}"
        for path, text in loaded_templates()
        for name, value in attributes_of(text)
        if name.startswith(SCRIPTED_PREFIXES) and VARIABLE.search(value)
    ]
    assert offenders == [], (
        "interpolate tenant data into a data-* attribute and read it with "
        "$el.dataset instead — never into an expression Alpine or HTMX evaluates"
    )
