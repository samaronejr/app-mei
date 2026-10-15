"""T45: AA ink on the R1 portal vault and account pages; flag-off bytes unchanged.

Root's axe run on the authenticated /documentos/ and /conta/ found five and three
serious color-contrast nodes at both widths. Every one was `ink-500` text: 4.46:1 on
the white of the header and the cards, 4.26:1 on the sunken page colour behind the
footer -- under AA for text this size. The two views passed no `cycles_r1`, so the
layout fell back to the legacy ink as well. `ink-600` measures 7.43:1 and 7.09:1 on
the same two grounds.

Under the flag these pages therefore draw no ink-500 text at all: neither the utility
nor a component whose own rule paints it (the size hint, the empty state) without the
R1 ink beside it. The browser sweep in tests/browser/test_a11y_axe.py measures the
same pages; this module pins the classes that make it pass.

AXR1-056 adds Pagamentos: final F3 found its empty state at 4.46:1 under the flag at
both widths, the one R1 portal page T45 did not cover.

Flag-off output stays byte for byte what the parent commit shipped, 4.46:1 included.
That legacy conflict is deliberate and left to the owner; nothing here claims a
flag-off page meets AA.
"""

import hashlib
import re
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Final, NamedTuple

import pytest
from django.template import Context, Engine
from django.test import Client, override_settings
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.obligations.models import Document, new_storage_key
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

# 4.46:1 on white, 4.26:1 on the sunken page colour.
LEGACY_INK: Final = "text-ink-500"
# 7.43:1 on white, 7.09:1 on the sunken page colour.
R1_INK: Final = "text-ink-600"
# Components whose own rule in assets/css/app.css paints ink-500.
INK_500_COMPONENTS: Final = frozenset({"field-help", "empty-state"})
CLASS_ATTRIBUTE: Final = re.compile(r'class="([^"]*)"')

# Each node root's axe run named, by the class attribute it carries under the flag.
HEADER_EMAIL: Final = 'class="ml-auto truncate text-ink-600"'
FOOTER: Final = 'class="mx-auto max-w-3xl px-5 pb-8 text-xs text-ink-600"'
SIZE_HINT: Final = 'class="field-help text-ink-600"'
ROW_META: Final = 'class="mt-1 text-xs tabular-nums text-ink-600"'
EMPTY_VAULT: Final = 'class="card empty-state text-ink-600"'
EMPTY_PAYMENTS: Final = EMPTY_VAULT
SIGNED_IN_AS: Final = '<dt class="text-ink-600">'


class Case(NamedTuple):
    route: str
    served_documents: int
    r1_nodes: tuple[str, ...]


CASES: Final = {
    "documents": Case(
        "portal-documents", 1, (HEADER_EMAIL, SIZE_HINT, ROW_META, FOOTER)
    ),
    "documents-empty": Case(
        "portal-documents", 0, (HEADER_EMAIL, SIZE_HINT, EMPTY_VAULT, FOOTER)
    ),
    "account": Case("portal-account", 0, (HEADER_EMAIL, SIGNED_IN_AS, FOOTER)),
    "payments-empty": Case(
        "portal-payments", 0, (HEADER_EMAIL, EMPTY_PAYMENTS, FOOTER)
    ),
}

# The flag-off reference: the parent and the three children as they were before this
# change. The layout is the final-T8 capture tests/portal/test_cycle_flag_off.py
# already compares against; the children were captured verbatim from db8b4a2 with
# git show, not derived from the templates under test.
LEGACY_TEMPLATES: Final = {
    "portal/base.html": "base_pre_cycles.html",
    "portal/documents.html": "documents_pre_r1_ink.html",
    "portal/partials/upload_form.html": "upload_form_pre_r1_ink.html",
    "portal/account.html": "account_pre_r1_ink.html",
    "portal/payments.html": "payments_pre_r1_ink.html",
}
PAGE_TEMPLATES: Final = {
    "portal-documents": "portal/documents.html",
    "portal-account": "portal/account.html",
    "portal-payments": "portal/payments.html",
}
CONTEXT_KEYS: Final = {
    "portal-documents": ("page", "documents", "max_size_label", "messages"),
    "portal-account": ("messages",),
    "portal-payments": ("page", "payments", "cycles_r1", "messages"),
}


@pytest.fixture(autouse=True)
def _portal_urls(settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]
    # Pinned to the shipped tree: sibling modules in this package point the middleware
    # at `tests.portal.urls`, which mounts probes instead of pages.
    monkeypatch.setattr("apps.portal.middleware.PORTAL_URLCONF", PORTAL_URLCONF)


def _open(case: str, features: list[str]) -> "_MonkeyPatchedWSGIResponse":
    """Seed one firm, one MEI client and its owner; GET the case's page as her."""
    route, documents, _ = CASES[case]
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
    for index in range(documents):
        payload = f"conteudo da nota {index}".encode()
        # A firm-published row is listed without an intake (T13), as in the vault suite.
        document = Document(
            tenant=tenant,
            client=client,
            sha256=hashlib.sha256(payload).hexdigest(),
            original_filename=f"nota-fiscal-{index:02d}.pdf",
            content_type="application/pdf",
            byte_size=len(payload),
            uploaded_by=owner,
            storage_key=new_storage_key(),
            client_upload=False,
        )
        with tenant_context(tenant.id):
            document.save(force_insert=True)

    http = Client()
    http.force_login(owner)
    response = http.get(
        reverse(route, urlconf=PORTAL_URLCONF), headers={"host": PORTAL_HOST}
    )
    assert response.status_code == HTTPStatus.OK, (
        f"{case} answered {response.status_code}, so nothing below is a rendered page"
    )
    return response


@pytest.mark.parametrize("case", sorted(CASES))
def test_the_r1_vault_and_account_draw_no_ink_under_aa(case: str) -> None:
    response = _open(case, [FLAG])
    body = response.content.decode()

    assert response.context.get(FLAG) is True, (
        f"{case} rendered without {FLAG}, so the layout draws the legacy ink"
    )
    missing = [node for node in CASES[case].r1_nodes if node not in body]
    assert missing == [], f"{case}: these nodes lack the R1 ink: {missing}"
    offenders = [
        classes
        for classes in CLASS_ATTRIBUTE.findall(body)
        if LEGACY_INK in classes.split()
        or (INK_500_COMPONENTS & set(classes.split()) and R1_INK not in classes.split())
    ]
    assert offenders == [], f"{case}: ink-500 text under {FLAG}: {offenders}"


@pytest.mark.parametrize("kill_switch", [False, True])
@pytest.mark.parametrize("case", sorted(CASES))
def test_flag_off_vault_and_account_keep_their_parent_bytes(
    settings: SettingsWrapper, case: str, kill_switch: bool
) -> None:
    settings.AXORIM_DISABLED_FEATURES = [FLAG] if kill_switch else []
    response = _open(case, [FLAG] if kill_switch else [])
    route = CASES[case].route

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
    context = {key: response.context[key] for key in CONTEXT_KEYS[route]}
    context.update(
        request=response.wsgi_request, csrf_token=str(response.context["csrf_token"])
    )
    with override_settings(ROOT_URLCONF=PORTAL_URLCONF):
        before = legacy.get_template(PAGE_TEMPLATES[route]).render(Context(context))
    assert response.content == before.encode()
