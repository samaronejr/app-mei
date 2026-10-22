"""AXRM-005 (T85): the vault refusal page names WHICH refusal happened.

`document_upload` hands `portal/upload_error.html` a machine reason -- `missing`,
`oversized`, `type_refused`, `scanner_off` -- and the page says the matching sentence.
Before, a refused type fell through to "Nenhum arquivo foi escolhido", the copy for a
missing file. For a tenant without `scan_required` the page stays byte for byte what
R1 rendered (the parent template, read verbatim from the pinned fixture).
"""

from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest
from django.template import Context, Engine
from django.test import override_settings
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.obligations.models import Document
from tests.portal.request_fixtures import PortalSetup, portal_setup, upload

if TYPE_CHECKING:
    from django.core.files.uploadedfile import SimpleUploadedFile

pytestmark = pytest.mark.django_db(transaction=True)

FLAG: Final = "scan_required"
CAP: Final = 4 * 1024
FIXTURES: Final[Path] = Path(__file__).parent / "fixtures"
COPY: Final = {
    "missing": "Nenhum arquivo foi escolhido. Escolha um arquivo e envie de novo.",
    "oversized": "O arquivo é maior que o limite de 4,0 kB. Envie um menor.",
    "type_refused": "Este arquivo não foi aceito. Envie um PDF, XML, PNG ou JPEG",
    "scanner_off": (
        "Envio temporariamente indisponível: a verificação antivírus está desligada."
    ),
}
HTML_BYTES: Final = b"<!doctype html><script>alert(1)</script>"
PDF_BYTES: Final = b"%PDF-1.4\n% recusa sintetica\n%%EOF\n"


@pytest.fixture
def setup(settings: SettingsWrapper, tmp_path: Path) -> PortalSetup:
    settings.MEDIA_ROOT = tmp_path
    settings.ALLOWED_HOSTS = [".localhost", "localhost", "testserver"]
    settings.PORTAL_UPLOAD_MAX_BYTES = CAP
    settings.CLAMAV_ENABLED = True
    return portal_setup("axrm005-reasons")


def _flagged(setup: PortalSetup) -> None:
    tenant = setup.graph.tenant
    tenant.features = [*tenant.features, FLAG]
    tenant.save(update_fields=["features"])


def _refuse(setup: PortalSetup, reason: str) -> Any:  # noqa: ANN401
    files: dict[str, SimpleUploadedFile] = {}
    if reason == "oversized":
        files["file"] = upload("grande.pdf", b"%PDF-1.4\n" + b"0" * CAP)
    elif reason == "type_refused":
        files["file"] = upload("pagina.pdf", HTML_BYTES)
    elif reason == "scanner_off":
        files["file"] = upload("nota.pdf", PDF_BYTES)
    response = setup.portal().post("/documentos/enviar", files)
    assert response.status_code == HTTPStatus.BAD_REQUEST, reason
    return response


def _documents(setup: PortalSetup) -> int:
    with tenant_context(setup.graph.tenant.pk):
        return Document.objects.count()


@pytest.mark.parametrize("reason", sorted(COPY))
def test_each_refusal_carries_its_machine_reason_and_its_own_sentence(
    setup: PortalSetup, settings: SettingsWrapper, reason: str
) -> None:
    _flagged(setup)
    if reason == "scanner_off":
        settings.CLAMAV_ENABLED = False

    response = _refuse(setup, reason)

    assert response.context["reason"] == reason
    body = response.content.decode()
    assert COPY[reason] in body
    others = [copy for key, copy in COPY.items() if key != reason]
    assert not [copy for copy in others if copy in body], (
        f"the {reason} page also says another refusal's sentence"
    )


def test_an_empty_file_is_the_missing_reason(setup: PortalSetup) -> None:
    _flagged(setup)
    response = setup.portal().post(
        "/documentos/enviar", {"file": upload("vazio.pdf", b"")}
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.context["reason"] == "missing"
    assert _documents(setup) == 0


def test_the_type_refusal_keeps_the_refused_original_and_offers_the_retry(
    setup: PortalSetup,
) -> None:
    _flagged(setup)
    body = _refuse(setup, "type_refused").content.decode()
    assert _documents(setup) == 1, "the refused original is retained (T12)"
    assert 'action="/documentos/enviar"' in body


def test_the_scanner_off_refusal_stores_nothing_and_offers_no_futile_retry(
    setup: PortalSetup, settings: SettingsWrapper
) -> None:
    _flagged(setup)
    settings.CLAMAV_ENABLED = False
    body = _refuse(setup, "scanner_off").content.decode()
    assert _documents(setup) == 0
    assert 'action="/documentos/enviar"' not in body
    assert 'href="/documentos/"' in body


@pytest.mark.parametrize("reason", ["missing", "oversized", "type_refused"])
def test_without_the_flag_the_page_keeps_its_r1_bytes(
    setup: PortalSetup, reason: str
) -> None:
    response = _refuse(setup, reason)

    engine = Engine.get_default()
    legacy = Engine(
        dirs=engine.dirs,
        libraries=engine.libraries,
        loaders=[
            (
                "django.template.loaders.locmem.Loader",
                {
                    "portal/upload_error.html": (
                        FIXTURES / "upload_error_r1.html"
                    ).read_text(),
                },
            ),
            "django.template.loaders.filesystem.Loader",
            "django.template.loaders.app_directories.Loader",
        ],
    )
    context = {
        key: response.context[key]
        for key in ("reason", "max_size_label", "messages", "cycles_r1")
    }
    context.update(
        request=response.wsgi_request, csrf_token=str(response.context["csrf_token"])
    )
    with override_settings(ROOT_URLCONF="apps.portal.urls"):
        before = legacy.get_template("portal/upload_error.html").render(
            Context(context)
        )
    assert response.content == before.encode()
