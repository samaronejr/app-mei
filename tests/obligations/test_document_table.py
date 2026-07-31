"""The document table's shape, and why each part of it is a control.

Three things here are enforced by the database rather than by application code, because
each of them fails silently if it is only a convention:

* `client_id NOT NULL` — PostgreSQL's default `MATCH SIMPLE` skips a composite foreign
  key check *entirely* when any key column is NULL, so a nullable column would leave the
  client-paired constraints T-070 adds present and inert.
* uniqueness leads with `client` — keyed on `(tenant, storage_key)` alone it would
answer
  "does this key exist anywhere in the firm", which is a one-bit existence oracle across
  clients.
* content-hash uniqueness is scoped to one client — firm-wide, a collision proves
another
  client holds a byte-identical document, which is the disclosure the portal exists to
  prevent, delivered through a constraint violation.
"""

import pytest
from django.db import IntegrityError, connection, transaction

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core import identifiers
from apps.core.tenancy import tenant_context
from apps.obligations.models import Document, new_storage_key
from apps.tenants.models import Tenant

pytestmark = pytest.mark.django_db

SHA = "a" * 64


class Fixture:
    """One firm and two of its clients."""

    def __init__(
        self, tenant: Tenant, alpha: ClientCompany, beta: ClientCompany, uploader: User
    ) -> None:
        self.tenant = tenant
        self.alpha = alpha
        self.beta = beta
        self.uploader = uploader


@pytest.fixture
def firm() -> Fixture:
    tenant = Tenant.objects.create(name="Acme Docs", slug="acme-docs")
    with tenant_context(tenant.id):
        # ALL_OBJECTS_OK: fixture seeding, before any portal context exists.
        alpha = ClientCompany.all_objects.create(
            tenant=tenant,
            legal_name="ALPHA",
            cnpj="11222333000181",
            is_mei=True,
        )
        # ALL_OBJECTS_OK: the sibling a client must never reach.
        beta = ClientCompany.all_objects.create(
            tenant=tenant,
            legal_name="BETA",
            cnpj="11444777000161",
            is_mei=True,
        )
    uploader = User.objects.create_user(email="up@acme-docs.example", password="x")  # noqa: S106
    return Fixture(tenant, alpha, beta, uploader)


def _document(firm: Fixture, client: ClientCompany, **overrides: object) -> Document:
    fields: dict[str, object] = {
        "tenant": firm.tenant,
        "client": client,
        "sha256": SHA,
        "original_filename": "das.pdf",
        "content_type": "application/pdf",
        "byte_size": 1024,
        "uploaded_by": firm.uploader,
    }
    fields.update(overrides)
    with tenant_context(firm.tenant.id):
        return Document.objects.create(**fields)


def test_the_client_column_is_not_null_in_the_database(firm: Fixture) -> None:
    # Given a direct INSERT that omits the client, bypassing every application check
    with (
        tenant_context(firm.tenant.id),
        pytest.raises(IntegrityError) as exc,
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            """
            INSERT INTO obligations_document
              (id, tenant_id, client_id, storage_key, sha256, original_filename,
               content_type, byte_size, uploaded_by_id, created_at)
            VALUES (%s, %s, NULL, %s, %s, 'x.pdf', 'application/pdf', 1, %s, now())
            """,
            [
                str(identifiers.uuid7()),
                str(firm.tenant.id),
                new_storage_key(),
                SHA,
                str(firm.uploader.pk),
            ],
        )

    # Then the database refuses it. Application-level validation would not survive a
    # data migration or a raw INSERT, and MATCH SIMPLE makes the composite FK inert
    # against a NULL — so this column being mandatory is what T-070 rests on.
    assert "client_id" in str(exc.value)


def test_the_primary_key_routes_through_the_locked_generator(firm: Fixture) -> None:
    # Given the model's pk default
    field = Document._meta.get_field("id")

    # Then it is the process-local locked wrapper, not uuid6's unguarded generator
    assert field.default is identifiers.uuid7

    document = _document(firm, firm.alpha)
    assert document.pk.version == 7


def test_the_storage_key_is_random_and_unguessable(firm: Fixture) -> None:
    # Given many keys drawn the way the model draws them
    keys = {new_storage_key() for _ in range(500)}

    # Then none repeats, and each carries enough entropy that enumeration is not a
    # strategy. Existence in the bucket is itself a cross-client disclosure.
    assert len(keys) == 500
    assert all(len(key) >= 40 for key in keys)


def test_the_storage_key_is_derived_from_nothing_about_the_document(
    firm: Fixture,
) -> None:
    # Given a document
    document = _document(firm, firm.alpha, original_filename="segredo.pdf")

    # Then nothing about it can be read out of the key, and no access decision may rest
    # on the key: the download path authorises against this ROW, under row security,
    # before it asks storage for anything.
    assert document.original_filename not in document.storage_key
    assert str(document.pk) not in document.storage_key
    assert document.sha256 not in document.storage_key
    assert str(document.client_id) not in document.storage_key


def test_two_clients_may_hold_byte_identical_documents(firm: Fixture) -> None:
    # Given the same content uploaded by two different clients of one firm
    _document(firm, firm.alpha)

    # When the second is stored
    other = _document(firm, firm.beta)

    # Then it is accepted. Firm-wide hash uniqueness would refuse this, and the refusal
    # would prove to one client that another holds the same file.
    assert other.sha256 == SHA


def test_one_client_may_not_store_the_same_content_twice(firm: Fixture) -> None:
    # Given a document already stored for this client
    _document(firm, firm.alpha)

    # When the same content is stored again for the SAME client
    # Then it is refused -- deduplication within one client discloses nothing
    with pytest.raises(IntegrityError), transaction.atomic():
        _document(firm, firm.alpha)


def test_an_obligation_is_optional(firm: Fixture) -> None:
    # Given a standalone upload, which the portal permits
    document = _document(firm, firm.alpha)

    # Then it stores without an obligation. The client-paired composite FK T-070 adds
    # binds only the attached case: MATCH SIMPLE skips when a key column is NULL, which
    # here means there is no reference to check.
    assert document.obligation_id is None


def test_the_table_is_client_scoped_and_tenant_scoped(firm: Fixture) -> None:
    # Given the shipped table
    columns = {
        field.column
        for field in Document._meta.get_fields()
        if hasattr(field, "column")
    }

    # Then it carries both dimensions, which is what makes it reachable by the portal
    # policy T-069 adds and by the tenant coverage meta-test
    assert "tenant_id" in columns
    assert "client_id" in columns
