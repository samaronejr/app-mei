"""T63: the R1 upload refusal draws the same AA ink as the vault it refuses from.

A refused upload answers 400 with `portal/upload_error.html`: the portal layout, a
pt-BR notice and the upload form again, so the client retries where she is. T45 gave
the vault page the R1 ink, but `_upload_refused` passed no `cycles_r1`, so the moment
an upload was refused the layout and the retry form fell back to the legacy ink: the
header email, the footer and the size hint at 4.46:1 on white and 4.26:1 on the
sunken page colour, under AA for text this size. `ink-600` measures 7.43:1 and 7.09:1.

Both refusals are covered, each at its pinned 400 and with its retry intact: a file
over the cap, and no file at all. tests/browser/test_portal_upload_refusal_axe.py
measures the same page with axe at both widths and then sends the corrected retry;
this module pins the classes and the contract that make it pass.

Flag-off output stays byte for byte what the parent commit shipped, 4.46:1 included.
That legacy conflict is deliberate and left to the owner; nothing here claims a
flag-off page meets AA.
"""

import re
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template import Context, Engine
from django.test import Client, override_settings
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.obligations.models import Document
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.support import enrol_totp

if TYPE_CHECKING:  # stubs-only: this type does not exist at runtime
    from django.test.client import _MonkeyPatchedWSGIResponse

pytestmark = pytest.mark.django_db(transaction=True)

PORTAL_URLCONF: Final = "apps.portal.urls"
PORTAL_HOST: Final = "acme-portal.localhost"
PASSWORD: Final = "sufficiently-long-passphrase"  # noqa: S105
FLAG: Final = "cycles_r1"
FIXTURES: Final[Path] = Path(__file__).parent / "fixtures"
REFUSALS: Final = ("missing", "oversized")

# Moved rather than restated, as the vault suite does: the retry's size hint must
# follow the setting the endpoint enforces, on the refusal page as on the vault.
SMALL_CAP: Final = 2 * 1024 * 1024
SMALL_CAP_LABEL: Final = "2,0 MB"

# 4.46:1 on white, 4.26:1 on the sunken page colour.
LEGACY_INK: Final = "text-ink-500"
# 7.43:1 on white, 7.09:1 on the sunken page colour.
R1_INK: Final = "text-ink-600"
# Components whose own rule in assets/css/app.css paints ink-500.
INK_500_COMPONENTS: Final = frozenset({"field-help", "empty-state"})
CLASS_ATTRIBUTE: Final = re.compile(r'class="([^"]*)"')

# The nodes the refusal page shares with the vault, by the class attribute each
# carries under the flag (tests/portal/test_portal_contrast.py names the same three).
HEADER_EMAIL: Final = 'class="ml-auto truncate text-ink-600"'
FOOTER: Final = 'class="mx-auto max-w-3xl px-5 pb-8 text-xs text-ink-600"'
SIZE_HINT: Final = 'class="field-help text-ink-600"'
R1_NODES: Final = (HEADER_EMAIL, SIZE_HINT, FOOTER)

# The flag-off reference: the layout as the final-T8 capture, the upload form as T45
# captured it, and the refusal page itself verbatim from a67e1e0 (git show), not
# derived from the templates under test.
LEGACY_TEMPLATES: Final = {
    "portal/base.html": "base_pre_cycles.html",
    "portal/upload_error.html": "upload_error_pre_r1_ink.html",
    "portal/partials/upload_form.html": "upload_form_pre_r1_ink.html",
}
CONTEXT_KEYS: Final = ("reason", "max_size_label", "messages")


@pytest.fixture(autouse=True)
def _portal_urls(settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]
    settings.PORTAL_UPLOAD_MAX_BYTES = SMALL_CAP
    # Pinned to the shipped tree: sibling modules in this package point the middleware
    # at `tests.portal.urls`, which mounts probes instead of pages.
    monkeypatch.setattr("apps.portal.middleware.PORTAL_URLCONF", PORTAL_URLCONF)


def _path(route: str) -> str:
    return str(reverse(route, urlconf=PORTAL_URLCONF))


def _refuse(
    refusal: str, features: list[str]
) -> tuple[Client, "_MonkeyPatchedWSGIResponse"]:
    """Seed one firm, one MEI client and its owner; have her upload refused."""
    tenant = Tenant.objects.create(name="Acme", slug="acme", features=features)
    with tenant_context(tenant.id):
        client = ClientCompany.objects.create(
            tenant=tenant,
            legal_name="CLIENTE ALPHA",
            cnpj="11222333000181",
            is_mei=True,
        )
    owner = User.objects.create_user(email="dono@mei.example", password=PASSWORD)
    enrol_totp(owner)
    Membership.objects.create(
        user=owner,
        tenant=tenant,
        role=TenantRole.CLIENT_OWNER,
        client=client,
    )

    http = Client()
    http.force_login(owner)
    payload: dict[str, SimpleUploadedFile] = {}
    if refusal == "oversized":
        payload["file"] = SimpleUploadedFile(
            "nota-grande.pdf", b"x" * (SMALL_CAP + 1), content_type="application/pdf"
        )
    response = http.post(
        _path("portal-document-upload"), payload, headers={"host": PORTAL_HOST}
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST, (
        f"the {refusal} upload answered {response.status_code}; the refusal is a "
        f"pinned 400"
    )
    with tenant_context(tenant.id):
        assert not Document.objects.filter(client=client).exists(), (
            f"the {refusal} refusal stored a document"
        )
    return http, response


@pytest.mark.parametrize("refusal", REFUSALS)
def test_the_r1_refusal_and_its_retry_draw_no_ink_under_aa(refusal: str) -> None:
    http, response = _refuse(refusal, [FLAG])
    body = response.content.decode()

    assert response.context.get(FLAG) is True, (
        f"the {refusal} refusal rendered without {FLAG}, so the layout and the retry "
        f"form draw the legacy ink"
    )
    missing = [node for node in R1_NODES if node not in body]
    assert missing == [], f"{refusal}: these nodes lack the R1 ink: {missing}"
    offenders = [
        classes
        for classes in CLASS_ATTRIBUTE.findall(body)
        if LEGACY_INK in classes.split()
        or (INK_500_COMPONENTS & set(classes.split()) and R1_INK not in classes.split())
    ]
    assert offenders == [], f"{refusal}: ink-500 text under {FLAG}: {offenders}"

    # The retry is still the whole control: the same endpoint, its own token, a
    # multipart body and the cap the endpoint applies.
    form_at = body.find(f'action="{_path("portal-document-upload")}"')
    assert form_at != -1, f"{refusal}: the refusal offers no retry"
    form = body[body.rfind("<form", 0, form_at) : body.find("</form>", form_at)]
    assert "csrfmiddlewaretoken" in form, "the retry would answer 403 on submit"
    assert 'enctype="multipart/form-data"' in form
    assert SMALL_CAP_LABEL in form, "the retry does not state the enforced cap"

    # And the way back is a link that answers.
    vault = _path("portal-documents")
    assert f'<a class="link" href="{vault}">' in body, f"{refusal}: no way back"
    assert http.get(vault, headers={"host": PORTAL_HOST}).status_code == HTTPStatus.OK


@pytest.mark.parametrize("kill_switch", [False, True])
@pytest.mark.parametrize("refusal", REFUSALS)
def test_a_flag_off_refusal_keeps_its_parent_bytes(
    settings: SettingsWrapper, refusal: str, kill_switch: bool
) -> None:
    settings.AXORIM_DISABLED_FEATURES = [FLAG] if kill_switch else []
    _, response = _refuse(refusal, [FLAG] if kill_switch else [])

    engine = Engine.get_default()
    legacy = Engine(
        dirs=engine.dirs,
        libraries=engine.libraries,
        loaders=[
            (
                "django.template.loaders.locmem.Loader",
                {
                    template: (FIXTURES / fixture).read_text()
                    for template, fixture in LEGACY_TEMPLATES.items()
                },
            ),
            "django.template.loaders.filesystem.Loader",
            "django.template.loaders.app_directories.Loader",
        ],
    )
    context = {key: response.context[key] for key in CONTEXT_KEYS}
    context.update(
        request=response.wsgi_request, csrf_token=str(response.context["csrf_token"])
    )
    with override_settings(ROOT_URLCONF=PORTAL_URLCONF):
        before = legacy.get_template("portal/upload_error.html").render(
            Context(context)
        )
    assert response.content == before.encode()
