"""Retained evidence cannot disappear through an ordinary bulk or raw delete."""

import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import DatabaseError, connection, transaction
from pytest_django.fixtures import SettingsWrapper

from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.cycles.errors import IllegalTransition, VersionConflict
from apps.cycles.models import EvidenceLink, ExtractedField, ServiceCycle
from apps.cycles.retention import place_hold, release_hold
from apps.cycles.retention_records import ContentPurgeClaim, LegalHold
from apps.cycles.retention_tasks import purge_tenant
from apps.cycles.services.cycle import open_cycle
from apps.cycles.services.requests import RequestFields, create_request
from apps.obligations.models import Document, Obligation, ObligationType
from tests.cycles.cycle_fixtures import CycleSetup, cycle_setup

pytestmark = pytest.mark.django_db(transaction=True)

NOW = datetime(2026, 10, 7, tzinfo=UTC)


def old_original(setup: CycleSetup) -> Document:
    kind, _ = ObligationType.objects.get_or_create(
        code="DAS", defaults={"name": "DAS", "periodicity": "monthly"}
    )
    subject = Obligation.objects.create(
        tenant=setup.tenant,
        client=setup.client,
        obligation_type=kind,
        competence_month=date(2018, 9, 1),
        nominal_due_date=date(2018, 10, 20),
        resolved_due_date=date(2018, 10, 20),
    )
    contents = b"retention source original"
    key = default_storage.save("retained/aged.pdf", ContentFile(contents))
    document = Document.objects.create(
        tenant=setup.tenant,
        client=setup.client,
        obligation=subject,
        storage_key=key,
        sha256=hashlib.sha256(contents).hexdigest(),
        original_filename="aged.pdf",
        content_type="application/pdf",
        byte_size=len(contents),
        uploaded_by=setup.actor,
    )
    ExtractedField.objects.create(
        tenant=setup.tenant,
        client=setup.client,
        document=document,
        name="valor_total",
        value="PRIVATE-DERIVED-CANARY",
        origin="pdf_text",
        source_span="private span",
        extractor_version="t18",
    )
    return document


def test_dry_run_counts_only_and_real_purge_erases_bytes_and_derived_text(
    settings: SettingsWrapper,
    tmp_path: Path,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    assert settings.RETENTION_PURGE_DRY_RUN is True
    setup = cycle_setup("retention-dry-run")
    with tenant_context(setup.tenant.pk):
        document = old_original(setup)
        digest = document.sha256
    assert purge_tenant(setup.tenant.pk, dry_run=True, now=NOW) == {
        "eligible": 1,
        "held": 0,
        "retained": 0,
        "purged": 0,
        "failed": 0,
    }
    assert default_storage.exists(document.storage_key)
    with tenant_context(setup.tenant.pk):
        assert not ContentPurgeClaim.objects.exists()
        assert (
            ExtractedField.objects.get(document=document).value
            == "PRIVATE-DERIVED-CANARY"
        )
    assert purge_tenant(setup.tenant.pk, dry_run=False, now=NOW)["purged"] == 1
    assert not default_storage.exists(document.storage_key)
    with tenant_context(setup.tenant.pk):
        document.refresh_from_db()
        assert document.purged_at == NOW
        assert document.sha256 == digest
        field = ExtractedField.objects.get(document=document)
        assert (field.value, field.source_span, field.redacted_at) == ("", "", NOW)
        assert ContentPurgeClaim.objects.get(content_id=document.pk).completed_at == NOW


def test_hold_blocks_purge_and_stale_release_writes_nothing(
    settings: SettingsWrapper,
    tmp_path: Path,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    setup = cycle_setup("retention-held")
    with tenant_context(setup.tenant.pk):
        document = old_original(setup)
        hold = place_hold(
            setup.actor,
            setup.client.pk,
            reason="Disputa em análise",
            expected_version=setup.client.version,
        )
        with pytest.raises(VersionConflict):
            release_hold(setup.actor, hold.pk, expected_version=hold.version + 1)
        assert LegalHold.objects.get(pk=hold.pk).released_at is None
    counts = purge_tenant(setup.tenant.pk, dry_run=False, now=NOW)
    assert (counts["held"], counts["purged"]) == (1, 0)
    assert default_storage.exists(document.storage_key)
    with tenant_context(setup.tenant.pk):
        assert not ContentPurgeClaim.objects.exists()
        release_hold(setup.actor, hold.pk, expected_version=hold.version)
    assert purge_tenant(setup.tenant.pk, dry_run=False, now=NOW)["purged"] == 1


def test_failed_storage_delete_keeps_claim_and_retries_without_loosening_hold(
    settings: SettingsWrapper,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    setup = cycle_setup("retention-retry")
    with tenant_context(setup.tenant.pk):
        document = old_original(setup)
    delete = default_storage.delete

    def refuse(key: str) -> None:
        assert not connection.in_atomic_block
        message = "synthetic storage refusal"
        raise OSError(message)

    monkeypatch.setattr(default_storage, "delete", refuse)
    assert purge_tenant(setup.tenant.pk, dry_run=False, now=NOW)["failed"] == 1
    with tenant_context(setup.tenant.pk):
        claim = ContentPurgeClaim.objects.get(content_id=document.pk)
        assert claim.completed_at is None
        assert claim.error_code == "storage_failed"
        with pytest.raises(IllegalTransition):
            place_hold(
                setup.actor,
                setup.client.pk,
                reason="Após a reivindicação",
                expected_version=setup.client.version,
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            ContentPurgeClaim.objects.filter(pk=claim.pk).update(
                storage_keys=["foreign/key"]
            )
        assert Document.objects.get(pk=document.pk).purged_at is None
    monkeypatch.setattr(default_storage, "delete", delete)
    assert purge_tenant(setup.tenant.pk, dry_run=False, now=NOW)["purged"] == 1
    assert not default_storage.exists(document.storage_key)


@pytest.mark.parametrize("raw", [False, True])
def test_fresh_standalone_original_cannot_be_deleted(
    settings: SettingsWrapper, tmp_path: Path, raw: bool
) -> None:
    settings.MEDIA_ROOT = tmp_path
    setup = cycle_setup("retention-backstop")
    contents = b"retention protected original"
    with tenant_context(setup.tenant.pk):
        key = default_storage.save("retained/original.bin", ContentFile(contents))
        document = Document.objects.create(
            tenant=setup.tenant,
            client=setup.client,
            storage_key=key,
            sha256="1" * 64,
            original_filename="original.bin",
            content_type="application/octet-stream",
            byte_size=len(contents),
            uploaded_by=setup.actor,
        )

        def remove() -> None:
            if raw:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM obligations_document WHERE id = %s",
                        [document.pk],
                    )
            else:
                Document.objects.filter(pk=document.pk).delete()

        with pytest.raises(DatabaseError), transaction.atomic():
            remove()
        assert Document.objects.filter(pk=document.pk).exists()
        assert (tmp_path / key).read_bytes() == contents


def test_hold_is_client_scoped_and_purge_cannot_cross_tenants(
    settings: SettingsWrapper,
    tmp_path: Path,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    own = cycle_setup("retention-scope")
    foreign = cycle_setup("retention-foreign")
    with tenant_context(own.tenant.pk):
        held = old_original(own)
        other_client = ClientCompany.objects.create(
            tenant=own.tenant, cnpj="11444777000161", legal_name="Sibling client"
        )
        sibling = old_original(replace(own, client=other_client))
        place_hold(
            own.actor,
            own.client.pk,
            reason="Retenção de um cliente",
            expected_version=own.client.version,
        )
    with tenant_context(foreign.tenant.pk):
        foreign_document = old_original(foreign)
    counts = purge_tenant(own.tenant.pk, dry_run=False, now=NOW)
    assert (counts["held"], counts["purged"]) == (1, 1)
    assert default_storage.exists(held.storage_key)
    assert not default_storage.exists(sibling.storage_key)
    assert default_storage.exists(foreign_document.storage_key)
    with tenant_context(foreign.tenant.pk):
        assert Document.objects.get(pk=foreign_document.pk).purged_at is None
        assert not ContentPurgeClaim.objects.exists()


def test_one_client_hold_preserves_shared_original_and_every_link(
    settings: SettingsWrapper,
    tmp_path: Path,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    setup = cycle_setup("retention-shared")
    with tenant_context(setup.tenant.pk):
        document = old_original(setup)
        for month in [9, 10]:
            ref = open_cycle(
                setup.actor,
                setup.engagement.pk,
                date(2026, month, 1),
                setup.definition.pk,
                idempotency_key=f"shared-{month}",
            )
            request = create_request(
                setup.actor,
                ref.id,
                RequestFields("Original compartilhado"),
                expected_version=ref.version,
            )
            EvidenceLink.objects.create(
                tenant=setup.tenant,
                client=setup.client,
                request=request,
                document=document,
                origin="staff",
                submitted_by=setup.actor,
            )
        hold = place_hold(
            setup.actor,
            setup.client.pk,
            reason="Preservar todos os vínculos",
            expected_version=setup.client.version,
        )
    after_fiscal_retention = datetime(2040, 1, 1, tzinfo=UTC)
    held = purge_tenant(setup.tenant.pk, dry_run=False, now=after_fiscal_retention)
    assert (held["held"], held["purged"]) == (1, 0)
    assert default_storage.exists(document.storage_key)
    with tenant_context(setup.tenant.pk):
        assert EvidenceLink.objects.filter(document=document).count() == 2
        release_hold(setup.actor, hold.pk, expected_version=hold.version)
    assert (
        purge_tenant(setup.tenant.pk, dry_run=False, now=after_fiscal_retention)[
            "purged"
        ]
        == 1
    )
    with tenant_context(setup.tenant.pk):
        assert EvidenceLink.objects.filter(document=document).count() == 2
        assert Document.objects.get(pk=document.pk).sha256 == document.sha256
    assert not default_storage.exists(document.storage_key)


def test_claim_rejects_another_clients_content_or_storage_descriptor(
    settings: SettingsWrapper,
    tmp_path: Path,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    setup = cycle_setup("retention-claim-scope")
    with tenant_context(setup.tenant.pk):
        document = old_original(setup)
        sibling = ClientCompany.objects.create(
            tenant=setup.tenant, cnpj="11444777000161", legal_name="Sibling"
        )
        for client, keys in [
            (sibling, [document.storage_key]),
            (setup.client, ["foreign/object"]),
        ]:
            with pytest.raises(DatabaseError), transaction.atomic():
                ContentPurgeClaim.objects.create(
                    tenant=setup.tenant,
                    client=client,
                    content_kind="document",
                    content_id=document.pk,
                    storage_keys=keys,
                )
        assert not ContentPurgeClaim.objects.exists()
    assert default_storage.exists(document.storage_key)


def test_new_link_cannot_extend_a_committed_deletion_claim(
    settings: SettingsWrapper,
    tmp_path: Path,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    setup = cycle_setup("retention-link-fence")
    with tenant_context(setup.tenant.pk):
        document = old_original(setup)
        ContentPurgeClaim.objects.create(
            tenant=setup.tenant,
            client=setup.client,
            content_kind="document",
            content_id=document.pk,
            storage_keys=[document.storage_key],
        )
    with tenant_context(setup.tenant.pk):
        ref = open_cycle(
            setup.actor,
            setup.engagement.pk,
            date(2026, 9, 1),
            setup.definition.pk,
            idempotency_key="claimed-original",
        )
        request = create_request(
            setup.actor,
            ref.id,
            RequestFields("Novo vínculo"),
            expected_version=ref.version,
        )
        with (
            pytest.raises(DatabaseError, match="no longer available"),
            transaction.atomic(),
        ):
            EvidenceLink.objects.create(
                tenant=setup.tenant,
                client=setup.client,
                request=request,
                document=document,
                origin="staff",
                submitted_by=setup.actor,
            )
        assert not EvidenceLink.objects.filter(document=document).exists()
        assert ServiceCycle.objects.filter(pk=ref.id).exists()
