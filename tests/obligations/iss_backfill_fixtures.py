"""Real database migration and command seams for ISS recovery tests."""

from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
from io import StringIO
from typing import TYPE_CHECKING

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from apps.core.retention import RetentionClass
from apps.core.tenancy import tenant_context
from apps.cycles.evidence import DocumentIntake
from apps.cycles.extraction.v2 import extract
from apps.cycles.scanning import FakeScanner, scan_intake
from apps.obligations.fiscal_ingest import _classify
from apps.obligations.models import AcquisitionOrigin, FiscalDocument
from apps.obligations.system_ingest import ingest_system_document
from tests.obligations.fiscal_fixtures import (
    KEY,
    LEGACY_ISS_DIGEST,
    nfse_with_retention,
    retention_element,
)
from tests.obligations.iss_writer_fixtures import no_vault_escape
from tests.obligations.test_fiscal_documents import ingest

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from uuid import UUID

    from tests.portal.request_fixtures import PortalSetup

BEFORE_ISS_PARTY = ("obligations", "0029_fiscal_register")
ADD_ISS_PARTY = ("obligations", "0030_iss_retention_party")


def _key(index: int) -> str:
    return "33" + f"{index:042d}"


def _payload(element: bytes, key: str = KEY) -> bytes:
    return nfse_with_retention(element).replace(KEY.encode(), key.encode(), 1)


def _ingest_retention_element(
    setup: PortalSetup, element: bytes, *, key: str = KEY, version: int = 1
) -> FiscalDocument:
    with no_vault_escape():
        result = ingest(
            setup,
            version=version,
            payload=_payload(element, key),
            client_role="tomador",
            source_namespace="focus:nfse_recebidas",
        )
    assert isinstance(result, FiscalDocument)
    return result


def _ingest_retention_note(
    setup: PortalSetup, code: str | None, *, key: str = KEY, version: int = 1
) -> FiscalDocument:
    return _ingest_retention_element(
        setup, retention_element(code), key=key, version=version
    )


@contextmanager
def migration_role() -> Iterator[None]:
    """Use the migration role while retaining the test connection."""
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE app_runtime")


@contextmanager
def at_0029() -> Iterator[list[tuple[str, str]]]:
    """Use the original fiscal schema; restore all migration leaves on exit."""
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    after = executor.loader.graph.leaf_nodes("obligations")
    try:
        executor.migrate([BEFORE_ISS_PARTY])
        yield after
    finally:
        MigrationExecutor(connection).migrate(leaves)


def seed_legacy_code_two_heads(setup: PortalSetup, keys: Iterable[str]) -> list[UUID]:
    """Record code-2 heads on 0029 with real, clean scoped XML evidence."""
    history = MigrationExecutor(connection).loader.project_state([BEFORE_ISS_PARTY])
    docs, revisions, artifacts = (
        history.apps.get_model("obligations", name)._base_manager
        for name in ("FiscalDocument", "FiscalRevision", "RevisionArtifact")
    )
    tenant, client = setup.graph.tenant.pk, setup.graph.client.pk
    scope = {"tenant_id": tenant, "client_id": client}
    heads = []
    for key in keys:
        payload = _payload(retention_element("2"), key)
        receipt = f"legacy-iss-{key}"
        original = ingest_system_document(
            tenant_id=tenant,
            client_id=client,
            origin=AcquisitionOrigin.PROVIDER_CAPTURE,
            receipt_ref=receipt,
            payload=payload,
            filename="documento-fiscal.xml",
            content_type="application/xml",
            retention_class=RetentionClass.FISCAL_EVIDENCE,
        )
        with tenant_context(tenant):
            intake = DocumentIntake.objects.get(document=original)
        scan_intake(tenant, intake.pk, scanner=FakeScanner(), now=timezone.now())
        with tenant_context(tenant):
            intake.refresh_from_db()
            _classify(original, intake, extract(intake, payload), "confirmed")
            head = docs.create(
                **scope,
                environment="fake",
                family="nfse_nacional",
                key_namespace="nacional",
                document_key=f"NFS{key}",
                client_role="tomador",
                source="captura",
                provider="focus",
                fiscal_state="autorizada",
                summary_only=False,
                role_verification="confirmed",
                iss_stated="12.34",
                iss_withheld_flag=True,
                iss_withheld_amount="12.34",
            )
            revision = revisions.create(
                **scope,
                document_id=head.pk,
                source_namespace="focus:nfse_recebidas",
                provider_version="1",
                # Only the canonical key is replayed; the paging heads need
                # distinct persisted revisions, not precomputed digests.
                semantic_digest=(
                    LEGACY_ISS_DIGEST["2"]
                    if key == KEY
                    else sha256(payload).hexdigest()
                ),
                fiscal_state="autorizada",
                receipt_ref=receipt,
            )
            artifacts.create(
                **scope,
                revision_id=revision.pk,
                document_id=original.pk,
                role="xml_original",
            )
            head.current_revision_id = revision.pk
            head.save(update_fields=["current_revision"])
            heads.append(head.pk)
    return heads


def run_iss_catch_up() -> str:
    """Exercise the real maintenance command with captured output."""
    stdout = StringIO()
    refusal = None
    try:
        call_command("backfill_iss_retention_party", stdout=stdout)
    except CommandError as error:
        refusal = error
    assert refusal is None, f"ISS catch-up did not run: {refusal}"
    return stdout.getvalue()
