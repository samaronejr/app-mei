"""A claim names its copies in the order the claim trigger compares (AXRM-051).

The trigger of `cycles` migration 0014 aggregates the expected keys with the
database's `ORDER BY key`. Under a linguistic collation such as `en_US.utf8` that
order differs from Python's code-point order, so the purge orders the keys in the
database too; a two-copy original is the case where the two orders disagree.
"""

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import DatabaseError
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.evidence import DocumentIntake, IntakeState, ScanVerdict
from apps.cycles.retention_records import ContentPurgeClaim
from apps.cycles.retention_tasks import purge_tenant
from apps.obligations.models import Document, Obligation, ObligationType
from tests.cycles.cycle_fixtures import cycle_setup

pytestmark = pytest.mark.django_db(transaction=True)


def test_a_claim_names_both_copies_in_the_order_the_database_compares(
    settings: SettingsWrapper, tmp_path: Path
) -> None:
    settings.MEDIA_ROOT = tmp_path
    setup = cycle_setup("claim-key-order")
    with tenant_context(setup.tenant.pk):
        kind, _ = ObligationType.objects.get_or_create(
            code="DAS", defaults={"name": "DAS", "periodicity": "monthly"}
        )
        obligation = Obligation.objects.create(
            tenant=setup.tenant,
            client=setup.client,
            obligation_type=kind,
            competence_month=date(2018, 9, 1),
            nominal_due_date=date(2018, 10, 20),
            resolved_due_date=date(2018, 10, 20),
        )
        contents = b"aged original AXRM-051"
        document = Document.objects.create(
            tenant=setup.tenant,
            client=setup.client,
            obligation=obligation,
            storage_key=default_storage.save(
                "Zz-final-original-051", ContentFile(contents)
            ),
            sha256=hashlib.sha256(contents).hexdigest(),
            original_filename="aged-051.pdf",
            content_type="application/pdf",
            byte_size=len(contents),
            uploaded_by=setup.actor,
        )
        held = default_storage.save(
            f"quarantine/{setup.tenant.pk}/{setup.client.pk}/held-051",
            ContentFile(b"held copy"),
        )
        DocumentIntake.objects.create(
            tenant=setup.tenant,
            client=setup.client,
            document=document,
            state=IntakeState.VERIFIED,
            verdict=ScanVerdict.CLEAN,
            quarantine_key=held,
            scanned_at=timezone.now(),
        )
    refused = ""
    counts: dict[str, int] = {}
    try:
        counts = purge_tenant(
            setup.tenant.pk, dry_run=False, now=datetime(2026, 10, 7, tzinfo=UTC)
        )
    except DatabaseError as error:
        refused = str(error)
    assert (refused, counts.get("purged")) == ("", 1)
    with tenant_context(setup.tenant.pk):
        claim = ContentPurgeClaim.objects.get(content_id=document.pk)
        assert set(claim.storage_keys) == {document.storage_key, held}
    assert not default_storage.exists(document.storage_key)
    assert not default_storage.exists(held)
