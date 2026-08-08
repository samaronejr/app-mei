"""The vendored front-end assets and the collectstatic step that hashes them."""

import hashlib
import json
import re
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.core.management import call_command
from django.template.loader import render_to_string
from django.test import Client, override_settings
from django.urls import reverse

from tests.ui.templates_scan import class_tokens, css_selector_for

STATIC_SOURCE = Path(settings.BASE_DIR) / "static"
VENDOR_MANIFEST = STATIC_SOURCE / "js" / "VENDOR.json"

# The evaluator that would force `script-src 'unsafe-eval'`. Alpine's ordinary build
# compiles every x-* expression with it; the CSP build ships an interpreter instead.
EVALUATOR = re.compile(r"new Function|\beval\(")


def _manifest() -> list[dict[str, str]]:
    entries: list[dict[str, str]] = json.loads(
        VENDOR_MANIFEST.read_text(encoding="utf-8"),
    )["vendored"]
    return entries


def test_vendored_bundles_are_committed() -> None:
    for entry in _manifest():
        assert (STATIC_SOURCE / "js" / entry["file"]).is_file()


def test_vendored_bundles_match_their_recorded_digest() -> None:
    """A hand-edited bundle is indistinguishable from a supply-chain edit."""
    for entry in _manifest():
        payload = (STATIC_SOURCE / "js" / entry["file"]).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == entry["sha256"], entry["file"]


def test_versions_are_pinned_exactly_in_package_json() -> None:
    package = json.loads(
        (Path(settings.BASE_DIR) / "package.json").read_text(encoding="utf-8"),
    )
    ranges = {
        name: spec
        for name, spec in package["devDependencies"].items()
        if not re.fullmatch(r"\d+\.\d+\.\d+", spec)
    }
    assert ranges == {}, "a range makes the committed bundle unreproducible"


def test_the_vendored_alpine_is_the_csp_build() -> None:
    """The whole policy rests on this file containing no evaluator.

    Swapping `@alpinejs/csp` for `alpinejs` would keep every other test green while
    silently requiring `'unsafe-eval'` in production.
    """
    entry = next(e for e in _manifest() if e["package"] == "@alpinejs/csp")
    source = (STATIC_SOURCE / "js" / entry["file"]).read_text(encoding="utf-8")
    assert EVALUATOR.search(source) is None


def test_the_compiled_stylesheet_is_committed_and_carries_the_theme() -> None:
    stylesheet = (STATIC_SOURCE / "css" / "app.css").read_text(encoding="utf-8")
    # A token from @theme and a component built from it: together they prove the
    # Tailwind build ran over this project's design system, not a default config.
    assert "--color-accent-600" in stylesheet
    assert "htmx-indicator" in stylesheet


def test_base_template_loads_both_bundles_from_this_origin_with_defer() -> None:
    html = render_to_string("base.html")
    for bundle in ("js/htmx.min.js", "js/alpine-csp.min.js"):
        tag = re.search(rf'<script src="([^"]*{re.escape(bundle)})"([^>]*)>', html)
        assert tag is not None, f"{bundle} is not loaded by base.html"
        assert tag.group(1).startswith(settings.STATIC_URL)
        assert "defer" in tag.group(2)


def test_base_template_disables_the_two_htmx_features_the_policy_forbids() -> None:
    html = render_to_string("base.html")
    config = re.search(r'name="htmx-config" content=\'([^\']+)\'', html)
    assert config is not None, "the htmx-config meta tag is missing"
    parsed: dict[str, Any] = json.loads(config.group(1))
    assert parsed["allowEval"] is False
    assert parsed["includeIndicatorStyles"] is False


@pytest.mark.django_db
def test_a_rendered_page_serves_the_bundles_from_this_origin() -> None:
    client = Client()
    response = client.get(reverse("dsr-submit"))
    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert f'src="{settings.STATIC_URL}js/htmx.min.js"' in body
    assert f'src="{settings.STATIC_URL}js/alpine-csp.min.js"' in body


def test_compiled_stylesheet_covers_every_class_the_templates_use() -> None:
    """A stale `static/css/app.css` is a silent, test-invisible regression.

    Tailwind emits only the utilities it finds in the scanned templates, so a class
    added to a template after the last `npm run build` simply has no rule. Nothing
    raises; the page renders unstyled. This is the only thing that notices.
    """
    stylesheet = (STATIC_SOURCE / "css" / "app.css").read_text(encoding="utf-8")
    missing = sorted(
        token for token in class_tokens() if css_selector_for(token) not in stylesheet
    )
    assert missing == [], "run `npm run build` — these classes have no compiled rule"


def test_collectstatic_produces_hashed_css_and_js(tmp_path: Path) -> None:
    """Prod serves through the manifest storage, so the hashing must actually work."""
    storages = {
        **settings.STORAGES,
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        },
    }
    with override_settings(STATIC_ROOT=tmp_path, STORAGES=storages):
        call_command("collectstatic", interactive=False, verbosity=0)
        manifest = json.loads(
            (tmp_path / "staticfiles.json").read_text(encoding="utf-8"),
        )["paths"]

    for logical in (
        "css/app.css",
        "js/htmx.min.js",
        "js/alpine-csp.min.js",
        "js/app.js",
    ):
        hashed = manifest[logical]
        assert hashed != logical, f"{logical} was not hashed"
        assert re.search(r"\.[0-9a-f]{12}\.", hashed), hashed
        assert (tmp_path / hashed).is_file()
