"""Upload and download, and the one property the bytes depend on.

For the document row every control this product has applies: a dedicated role, a GUC, a
RESTRICTIVE policy, a client-paired foreign key, client-leading uniqueness. For the
BYTES there is exactly one layer, and it is the Django view. So the assertion that
matters here is not that a replayed key is refused -- it is that storage is never asked.

**W6 is asserted by spying on the storage client.** Under `FileSystemStorage`, which is
what tests run on, `open()` is lazy: "before any byte is read" is true no matter what
the code does, so an assertion phrased that way cannot fail and would be decoration.
Asserting the client was never *invoked* distinguishes "refused before fetching" from
"fetched, then refused" -- and under object storage those differ by a real network call
that has already left the building.
"""

import hashlib
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING
from unittest import mock

import pytest
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from pytest_django.fixtures import SettingsWrapper

if TYPE_CHECKING:  # stubs-only: this type does not exist at runtime
    from django.test.client import _MonkeyPatchedWSGIResponse

from apps.accounts.models import User
from apps.audit.models import AccessLog
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.obligations.models import Document, new_storage_key
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.support import enrol_totp

pytestmark = pytest.mark.django_db(transaction=True)

PORTAL_HOST = "acme-portal.localhost"
PASSWORD = "irrelevant-here"  # noqa: S105
PAYLOAD = b"fiscal evidence"


@dataclass(frozen=True)
class Vault:
    """One firm, two clients, and a stored document for each."""

    tenant: Tenant
    alpha: ClientCompany
    beta: ClientCompany
    alpha_user: User
    alpha_doc: Document
    beta_doc: Document


@pytest.fixture(autouse=True)
def _portal_urls(settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]
    monkeypatch.setattr("apps.portal.middleware.PORTAL_URLCONF", "apps.portal.urls")


def _store(
    tenant: Tenant, client: ClientCompany, user: User, marker: bytes
) -> Document:
    document = Document(
        tenant=tenant,
        client=client,
        sha256=hashlib.sha256(marker).hexdigest(),
        original_filename="das.pdf",
        content_type="application/pdf",
        byte_size=len(marker),
        uploaded_by=user,
        storage_key=new_storage_key(),
    )
    default_storage.save(document.storage_key, __import__("io").BytesIO(marker))
    with tenant_context(tenant.id):
        document.save(force_insert=True)
    return document


@pytest.fixture
def vault() -> Vault:
    tenant = Tenant.objects.create(name="Acme", slug="acme")
    with tenant_context(tenant.id):
        # ALL_OBJECTS_OK: fixture seeding, before any portal context exists.
        alpha = ClientCompany.all_objects.create(
            tenant=tenant,
            legal_name="ALPHA",
            cnpj="11222333000181",
            is_mei=True,
        )
        # ALL_OBJECTS_OK: the sibling whose bytes must be unreachable.
        beta = ClientCompany.all_objects.create(
            tenant=tenant,
            legal_name="BETA",
            cnpj="11444777000161",
            is_mei=True,
        )
    alpha_user = User.objects.create_user(email="a@acme.example", password=PASSWORD)
    beta_user = User.objects.create_user(email="b@acme.example", password=PASSWORD)
    enrol_totp(alpha_user)
    enrol_totp(beta_user)
    Membership.objects.create(
        user=alpha_user,
        tenant=tenant,
        role=TenantRole.CLIENT_OWNER,
        client=alpha,
    )
    Membership.objects.create(
        user=beta_user,
        tenant=tenant,
        role=TenantRole.CLIENT_OWNER,
        client=beta,
    )
    return Vault(
        tenant,
        alpha,
        beta,
        alpha_user,
        _store(tenant, alpha, alpha_user, PAYLOAD),
        _store(tenant, beta, beta_user, b"beta's evidence"),
    )


def _as_alpha(vault: Vault) -> Client:
    http = Client()
    http.force_login(vault.alpha_user)
    return http


def _download(http: Client, key: str) -> "_MonkeyPatchedWSGIResponse":
    # The path literal rather than reverse(): the portal urlconf is installed by
    # PortalMiddleware per request, so reverse() resolves against ROOT_URLCONF and does
    # not see these names.
    return http.get(f"/documentos/{key}", headers={"host": PORTAL_HOST})


def test_a_client_downloads_its_own_document(vault: Vault) -> None:
    # Given a portal user with a document of its own
    response = _download(_as_alpha(vault), vault.alpha_doc.storage_key)

    # Then the bytes come back, in memory rather than as a stream -- PortalMiddleware
    # refuses a StreamingHttpResponse, whose iterator would be drained after the role
    # and both GUCs are gone
    assert response.status_code == HTTPStatus.OK
    assert response.content == PAYLOAD
    assert not hasattr(response, "streaming_content")


def test_w6_another_clients_key_is_refused_without_asking_storage(
    vault: Vault,
) -> None:
    # Given beta's storage key, which a caller can hold without ever having read the row
    http = _as_alpha(vault)

    # When alpha replays it, with the storage client watched
    with mock.patch.object(
        default_storage,
        "open",
        side_effect=AssertionError("storage was asked for bytes"),
    ) as opened:
        response = _download(http, vault.beta_doc.storage_key)

    # Then it is refused by the ROW lookup -- the RESTRICTIVE policy matches no row for
    # this client -- and storage was never invoked. That ordering is the requirement:
    # fetching first and refusing afterwards is a materially weaker property, and under
    # object storage the difference is a network call that already happened.
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert opened.call_count == 0


def test_w6_the_positive_control_reaches_storage_for_its_own_key(
    vault: Vault,
) -> None:
    # Given the identical request path, differing only in whose key it names
    http = _as_alpha(vault)

    with mock.patch.object(
        default_storage,
        "open",
        wraps=default_storage.open,
    ) as opened:
        response = _download(http, vault.alpha_doc.storage_key)

    # Then storage IS asked. Without this the refusal above would also pass against a
    # view that never touches storage at all -- including one that is simply broken.
    assert response.status_code == HTTPStatus.OK
    assert opened.call_count == 1


def test_c5_a_download_is_recorded_in_the_access_log(vault: Vault) -> None:
    # Given the Marco Civil log, which no longer exempts the media prefix
    before = AccessLog.objects.count()

    # When a document is fetched
    assert _download(_as_alpha(vault), vault.alpha_doc.storage_key).status_code == (
        HTTPStatus.OK
    )

    # Then the fetch is recorded. This is the leg T-067 could not assert, because the
    # view did not exist: downloads are exactly the events an investigation asks for.
    assert AccessLog.objects.count() == before + 1


def test_an_oversized_document_is_refused_by_the_cap(
    vault: Vault,
    settings: SettingsWrapper,
) -> None:
    # Given a cap below the stored document's size
    settings.PORTAL_DOCUMENT_MAX_BYTES = 1

    # When it is requested
    response = _download(_as_alpha(vault), vault.alpha_doc.storage_key)

    # Then it is refused cleanly rather than buffered and then refused, and never with a
    # MemoryError
    assert response.status_code == HTTPStatus.BAD_REQUEST


def _upload(
    http: Client, content: bytes, name: str = "das.pdf"
) -> "_MonkeyPatchedWSGIResponse":
    return http.post(
        "/documentos/enviar",
        {"file": SimpleUploadedFile(name, content, content_type="application/pdf")},
        headers={"host": PORTAL_HOST},
    )


def test_t079_an_upload_is_attributed_to_the_uploaders_own_client(
    vault: Vault,
) -> None:
    # Given a portal user uploading a file. Counted INSIDE a tenant context: outside one
    # the policy hides every row and `before` reads 0, so the assertion compares a
    # filtered count to an unfiltered one.
    with tenant_context(vault.tenant.id):
        before = Document.objects.count()
    response = _upload(_as_alpha(vault), b"novo documento")

    # Then it is stored against THEIR client and no other. The row is attributed from
    # request.client rather than the request body, so there is nothing to forge -- and
    # the WITH CHECK on this table would refuse a forgery anyway.
    assert response.status_code == HTTPStatus.FOUND, response.status_code
    with tenant_context(vault.tenant.id):
        stored = Document.objects.order_by("-created_at").first()
        assert Document.objects.count() == before + 1
        assert stored is not None
        assert stored.client_id == vault.alpha.id
        assert stored.uploaded_by_id == vault.alpha_user.pk


def test_t079_the_ingest_cap_refuses_an_oversized_upload(
    vault: Vault,
    settings: SettingsWrapper,
) -> None:
    # Given a cap of one byte
    settings.PORTAL_UPLOAD_MAX_BYTES = 1
    with tenant_context(vault.tenant.id):
        before = Document.objects.count()

    # When a larger file is sent
    response = _upload(_as_alpha(vault), b"muito grande")

    # Then it is refused and nothing is stored. Django's own defaults bound only what is
    # held in memory before spooling to disk, not the total, so without this cap an
    # authenticated caller can fill the container's filesystem.
    assert response.status_code == HTTPStatus.BAD_REQUEST
    with tenant_context(vault.tenant.id):
        assert Document.objects.count() == before


def test_t079_a_request_with_no_file_is_refused(vault: Vault) -> None:
    response = _as_alpha(vault).post(
        "/documentos/enviar",
        {},
        headers={"host": PORTAL_HOST},
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
