"""The operator's retention inventory reads everything and changes nothing (AXRM-051).

A SITFIS report drives a real federal original through the inventory command: its
dating references, every stored version and its expiry, holds, completed and
unproved disposals, rows whose object is missing without proof, federal records by
field class, authority evidence and the provider journal. Versioned-bucket rules
are read back through a fake S3 client; an unreadable or unfamiliar rule leaves
the listing INCOMPLETE rather than COMPLETE.
"""

from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from botocore.exceptions import ClientError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper
from storages.backends.s3 import S3Storage

from apps.core.tenancy import tenant_context
from apps.cycles import retention, retention_inventory, retention_tasks
from apps.cycles.evidence import DocumentIntake, IntakeState, ScanVerdict
from apps.cycles.retention_records import ContentPurgeClaim
from apps.cycles.retention_tasks import purge_tenant
from apps.obligations.models import AcquisitionOrigin, Document, FiscalSituationReport
from tests.integrations.sitfis_support import (
    PDF,
    World,
    clock,
    protocol_reply,
    report_reply,
    world,
)
from tests.ui.factories import make_cnpj, make_firm

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["clock", "world"]

SAO_PAULO = ZoneInfo("America/Sao_Paulo")
T0 = datetime(2026, 1, 10, tzinfo=UTC)


def _emit(world: World) -> FiscalSituationReport:
    world.script(protocol_reply(30_000))
    report = world.advance(world.request())
    world.clock.advance(seconds=30)
    world.script(report_reply())
    emitted = world.advance(report)
    world.scan()
    return world.reload(emitted)


def _original(world: World, report: FiscalSituationReport) -> Document:
    assert report.document_id is not None
    with tenant_context(world.tenant_id):
        return Document.objects.get(pk=report.document_id)


def _expiry(report: FiscalSituationReport) -> datetime:
    assert report.emitted_at is not None
    year = timezone.localdate(report.emitted_at).year + 6
    return datetime(year, 1, 1, tzinfo=SAO_PAULO)


def _inventory(world: World, *extra: str) -> list[str]:
    out = StringIO()
    call_command(
        "retention_inventory", "--tenant", str(world.tenant_id), *extra, stdout=out
    )
    return out.getvalue().splitlines()


def _listing(world: World) -> list[str]:
    """Return the listing, or the escape itself as its one line."""
    try:
        return _inventory(world)
    except Exception as error:  # noqa: BLE001 - an escape is itself the failure
        return [f"raised {error!r}"]


def _with(lines: list[str], *parts: str) -> list[str]:
    return [line for line in lines if all(part in line for part in parts)]


def _one(lines: list[str], *parts: str) -> str:
    found = _with(lines, *parts)
    assert len(found) == 1, found
    return found[0]


def test_a_dry_run_accounts_for_an_unlinked_report_with_each_version(
    world: World, settings: SettingsWrapper
) -> None:
    settings.RETENTION_PURGE_DRY_RUN = False  # read-only whatever the switch says
    report = _emit(world)
    original = _original(world, report)
    lines = _inventory(world)
    item = _one(lines, "ITEM kind=document", f"id={original.pk}")
    assert (
        f"emissao_relatorio:{timezone.localdate(report.emitted_at)}:{report.pk}" in item
    )
    for field in ("gap=-", "refusal=-", "claim=none"):
        assert field in item
    expires = timezone.localtime(_expiry(report)).isoformat()
    assert f"expires={expires}" in item
    version = _one(lines, "VERSION", f"key={original.storage_key}", "version=current")
    assert f"expires={expires}" in version
    assert _with(lines, "JOURNAL scope=client", "key=provider-journal/")
    assert _with(lines, "RECORD kind=federal_payment", "amounts=pending-og08")
    assert lines[-1] == "COMPLETE deleted=0"
    # Nothing was claimed or deleted.
    assert default_storage.exists(original.storage_key)
    with tenant_context(world.tenant_id):
        assert not ContentPurgeClaim.objects.exists()


def test_purge_is_refused_unless_the_switch_is_off_and_no_client_is_named(
    world: World, settings: SettingsWrapper
) -> None:
    report = _emit(world)
    settings.RETENTION_PURGE_DRY_RUN = True
    with pytest.raises(CommandError, match="RETENTION_PURGE_DRY_RUN"):
        _inventory(world, "--purge")
    settings.RETENTION_PURGE_DRY_RUN = False
    with pytest.raises(CommandError, match="--client"):
        _inventory(world, "--purge", "--client", str(world.termo.client.pk))
    with tenant_context(world.tenant_id):
        assert not ContentPurgeClaim.objects.exists()
    assert default_storage.exists(_original(world, report).storage_key)
    # Switch off and no client: the tenant's live purge runs (nothing expired yet).
    assert _inventory(world, "--purge")[0].startswith("PURGE eligible=0 ")


def test_a_missing_object_needs_a_confirmed_disposition(world: World) -> None:
    report = _emit(world)
    original = _original(world, report)
    # A restore that predates the disposal: the row is back, the object is not.
    default_storage.delete(original.storage_key)
    lines = _inventory(world)
    assert _with(lines, "MISSING kind=document", f"id={original.pk}", "proof=none")
    assert lines[-1] == "INCOMPLETE deleted=0 reasons=missing-without-proof"
    # A disposal the purge proved and completed accounts for the absence.
    default_storage.save(original.storage_key, ContentFile(PDF))
    assert (
        purge_tenant(world.tenant_id, dry_run=False, now=_expiry(report))["purged"] == 1
    )
    lines = _inventory(world)
    assert _with(
        lines, "DISPOSED kind=document", f"id={original.pk}", "disposition=confirmed"
    )
    assert lines[-1] == "COMPLETE deleted=0"
    # Bytes back at a disposed key are not a confirmed disposition.
    default_storage.save(original.storage_key, ContentFile(b"restored copy"))
    lines = _inventory(world)
    assert _with(lines, "DISPOSED", f"id={original.pk}", "disposition=unproved")
    assert lines[-1] == "INCOMPLETE deleted=0 reasons=disposition-unproved"


def test_a_client_listing_shows_nothing_of_the_firm_or_a_sibling(world: World) -> None:
    _emit(world)
    with tenant_context(world.tenant_id):
        sibling = world.termo.client.__class__.objects.create(
            tenant=world.termo.tenant, cnpj="11444777000161", legal_name="Sibling"
        )
    lines = _inventory(world, "--client", str(sibling.pk))
    assert not _with(lines, "ITEM")
    assert not _with(lines, "JOURNAL scope=client key=")
    assert not _with(lines, "firm_authorization")
    assert _with(lines, "JOURNAL scope=tenant omitted-for-client")


class FakeClient:
    """The two boto3 calls the inventory makes, answered from fixed pages."""

    def __init__(
        self, lifecycle: dict[str, Any] | None, pages: list[dict[str, Any]]
    ) -> None:
        self.lifecycle = lifecycle
        self.pages = pages

    def get_bucket_lifecycle_configuration(self, **_kwargs: object) -> dict[str, Any]:
        if self.lifecycle is None:
            error = {"Error": {"Code": "NoSuchLifecycleConfiguration"}}
            raise ClientError(error, "GetBucketLifecycleConfiguration")  # type: ignore[arg-type]
        return self.lifecycle

    def get_paginator(self, _name: str) -> "FakeClient":
        return self

    def paginate(self, **_kwargs: object) -> list[dict[str, Any]]:
        return self.pages


class FakeBucket(S3Storage):
    """An S3 backend whose boto3 client is a FakeClient; it sends nothing."""

    def __init__(self, client: FakeClient, location: str = "") -> None:
        super().__init__(
            location=location,
            bucket_name="axrm051-synthetic",
            endpoint_url="https://storage.invalid",
            access_key="synthetic-access",
            secret_key="synthetic-secret",  # noqa: S106 - no request is ever sent
        )
        self._fake = client

    @property
    def connection(self) -> Any:  # noqa: ANN401 - the boto3 resource shape
        return type("Resource", (), {"meta": type("Meta", (), {"client": self._fake})})


def _version(key: str, version: str, *, latest: bool, at: datetime) -> dict[str, Any]:
    return {"Key": key, "VersionId": version, "IsLatest": latest, "LastModified": at}


@pytest.mark.parametrize(
    ("rules", "state"),
    [
        (None, "unverified"),
        (
            {
                "Rules": [
                    {
                        "Status": "Enabled",
                        "Filter": {"Prefix": "originals/"},
                        "NoncurrentVersionExpiration": {
                            "NoncurrentDays": 30,
                            "NewerNoncurrentVersions": 2,
                        },
                    }
                ]
            },
            "unknown",
        ),
        (
            {
                "Rules": [
                    {
                        "Status": "Enabled",
                        "Filter": {"Tag": {"Key": "k", "Value": "v"}},
                        "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
                    }
                ]
            },
            "unknown",
        ),
        (
            # An action this module does not read may remove versions.
            {
                "Rules": [
                    {
                        "Status": "Enabled",
                        "Filter": {"Prefix": ""},
                        "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
                        "FutureVersionRemoval": {"Days": 1},
                    }
                ]
            },
            "unknown",
        ),
        (
            {
                "Rules": [
                    {
                        "Status": "Enabled",
                        "Filter": {"Prefix": ""},
                        "Expiration": {"Days": 365, "Unread": True},
                    }
                ]
            },
            "unknown",
        ),
        (
            {
                "Rules": [
                    {
                        "Status": "Enabled",
                        "Filter": {"Prefix": ""},
                        "Expiration": {"Date": "2027-01-01T00:00:00Z"},
                    }
                ]
            },
            "unknown",
        ),
    ],
)
def test_an_unreadable_or_unfamiliar_rule_is_never_no_rule(
    rules: dict[str, Any] | None, state: str
) -> None:
    key = "originals/a.pdf"
    pages = [{"Versions": [_version(key, "v2", latest=True, at=T0)]}]
    bucket = FakeBucket(FakeClient(rules, pages))
    assert retention_inventory.lifecycle(bucket).state == state
    old = {"Versions": [_version(key, "v1", latest=False, at=T0 - timedelta(days=9))]}
    listed = retention_inventory.versions(
        bucket, key, retention_inventory.lifecycle(bucket), "2032-01-01"
    )
    assert [v.expires for v in listed] == ["2032-01-01"]
    listed = retention_inventory.versions(
        FakeBucket(FakeClient(rules, [*pages, old])),
        key,
        retention_inventory.lifecycle(bucket),
        "2032-01-01",
    )
    assert listed[1].expires == f"lifecycle-{state}"


def test_a_verified_rule_dates_each_noncurrent_version_from_its_successor() -> None:
    key = "originals/a.pdf"
    rules = {
        "Rules": [
            {
                "Status": "Enabled",
                "Filter": {"Prefix": "originals/"},
                "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
            }
        ]
    }
    pages = [
        {
            "Versions": [
                _version(key, "v3", latest=True, at=T0),
                _version("originals/a.pdf.bak", "x1", latest=True, at=T0),
            ]
        },
        {
            "Versions": [
                _version(key, "v2", latest=False, at=T0 - timedelta(days=5)),
                _version(key, "v1", latest=False, at=T0 - timedelta(days=9)),
            ]
        },
    ]
    bucket = FakeBucket(FakeClient(rules, pages))
    state = retention_inventory.lifecycle(bucket)
    assert (state.state, state.rules) == ("verified", (("originals/", 30),))
    listed = retention_inventory.versions(bucket, key, state, "2032-01-01")
    # The sibling key sharing the prefix is not listed; pages are joined.
    assert [v.version for v in listed] == ["v3", "v2", "v1"]
    month = timedelta(days=30)
    eligible = [T0 + month, T0 - timedelta(days=5) + month]
    assert [v.expires for v in listed] == [
        "2032-01-01",
        *(f"eligible-from:{timezone.localtime(at).isoformat()}" for at in eligible),
    ]


@pytest.mark.parametrize(
    "expiration", [{"Days": 365}, {"Date": datetime(2027, 1, 1, tzinfo=UTC)}]
)
def test_a_rule_that_expires_live_versions_dates_them_by_it(
    expiration: dict[str, Any],
) -> None:
    key = "originals/a.pdf"
    rules = {
        "Rules": [
            {"Status": "Enabled", "Filter": {"Prefix": ""}, "Expiration": expiration}
        ]
    }
    bucket = FakeBucket(
        FakeClient(rules, [{"Versions": [_version(key, "v1", latest=True, at=T0)]}])
    )
    state = retention_inventory.lifecycle(bucket)
    assert state.state == "verified"
    (listed,) = retention_inventory.versions(bucket, key, state, "2032-01-01")
    # The bucket removes the live version then, whatever its fiscal date.
    removed = expiration.get("Date") or T0 + timedelta(days=365)
    assert listed.expires == f"lifecycle-from:{timezone.localtime(removed).isoformat()}"


def test_actions_that_remove_no_dated_version_leave_the_rule_verified() -> None:
    key = "originals/a.pdf"
    rules = {
        "Rules": [
            {
                "ID": "storage-class",
                "Status": "Enabled",
                "Filter": {"Prefix": ""},
                "Transitions": [{"Days": 30, "StorageClass": "GLACIER"}],
                "Expiration": {"ExpiredObjectDeleteMarker": True},
                "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
            }
        ]
    }
    bucket = FakeBucket(
        FakeClient(rules, [{"Versions": [_version(key, "v1", latest=True, at=T0)]}])
    )
    state = retention_inventory.lifecycle(bucket)
    assert state.state == "verified"
    (listed,) = retention_inventory.versions(bucket, key, state, "2032-01-01")
    assert listed.expires == "2032-01-01"


@pytest.mark.parametrize(
    ("location", "spelled", "resolved", "prefix"),
    [
        ("", "/originals/a.pdf", "originals/a.pdf", "originals/"),
        ("media", "originals/a.pdf", "media/originals/a.pdf", "media/originals/"),
    ],
)
def test_a_rule_matches_the_key_the_backend_resolves(
    location: str, spelled: str, resolved: str, prefix: str
) -> None:
    rules = {
        "Rules": [
            {
                "Status": "Enabled",
                "Filter": {"Prefix": prefix},
                "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
            }
        ]
    }
    pages = [
        {
            "Versions": [
                _version(resolved, "v2", latest=True, at=T0),
                _version(resolved, "v1", latest=False, at=T0 - timedelta(days=9)),
            ]
        }
    ]
    bucket = FakeBucket(FakeClient(rules, pages), location=location)
    state = retention_inventory.lifecycle(bucket)
    listed = retention_inventory.versions(bucket, spelled, state, "2032-01-01")
    eligible = timezone.localtime(T0 + timedelta(days=30)).isoformat()
    assert [v.expires for v in listed] == ["2032-01-01", f"eligible-from:{eligible}"]


def test_a_delete_marker_above_older_versions_is_a_missing_original(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _original(world, _emit(world))
    with tenant_context(world.tenant_id):
        intake = DocumentIntake.objects.get(document=original)
    # A restore that predates the disposal, on a versioned bucket: each key's
    # latest version is a delete marker above the older bytes.
    pages = [
        {
            "Versions": [_version(key, "v1", latest=False, at=T0)],
            "DeleteMarkers": [
                _version(key, "d1", latest=True, at=T0 + timedelta(days=1))
            ],
        }
        for key in (original.storage_key, intake.quarantine_key)
    ]
    monkeypatch.setattr(
        retention_inventory, "default_storage", FakeBucket(FakeClient(None, pages))
    )
    lines = _inventory(world)
    assert _with(lines, "MISSING kind=document", f"id={original.pk}", "proof=none")
    assert "missing-without-proof" in lines[-1]


def test_an_unverified_bucket_leaves_the_inventory_incomplete(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _emit(world)
    bucket = FakeBucket(FakeClient(None, [{"Versions": []}]))
    monkeypatch.setattr(retention_inventory, "default_storage", bucket)
    lines = _inventory(world)
    assert "lifecycle=unverified" in lines[0]
    assert lines[-1].startswith("INCOMPLETE deleted=0 reasons=")
    assert "lifecycle-unverified" in lines[-1]


@pytest.mark.parametrize("owner", ["other-tenant", "this-tenant"])
@pytest.mark.parametrize("spelling", ["exact", "doubled", "parent"])
def test_a_shared_key_is_refused_as_the_purge_refuses_it(
    world: World, owner: str, spelling: str
) -> None:
    report = _emit(world)
    original = _original(world, report)
    key = original.storage_key
    # Any spelling that resolves to the same object is the same key; a final is a
    # single root segment, so each spelling is built to change one too.
    named = {
        "exact": key,
        "doubled": f".//{key}",
        "parent": f"restored/../{key}",
    }[spelling]
    if owner == "other-tenant":
        firm = make_firm(f"axrm051-inventory-{spelling}", client_count=1)
        tenant, client, actor = firm.tenant, firm.clients[0], firm.owner
    else:
        # Another client of the same tenant: one client never holds one key twice.
        tenant, actor = world.termo.tenant, world.termo.actor
        with tenant_context(tenant.pk):
            client = world.termo.client.__class__.objects.create(
                tenant=tenant,
                cnpj=make_cnpj(f"5105105100{len(spelling):02d}"),
                legal_name="Irmã",
            )
    with tenant_context(tenant.pk):
        Document.objects.create(
            tenant=tenant,
            client=client,
            storage_key=named,
            sha256=original.sha256,
            original_filename="alheio.pdf",
            content_type="application/pdf",
            byte_size=original.byte_size,
            uploaded_by=actor,
            acquisition_origin=AcquisitionOrigin.UPLOAD_STAFF,
            portal_visible=False,
        )
    item = _one(_inventory(world), "ITEM kind=document", f"id={original.pk}")
    assert "refusal=chave_compartilhada" in item


def test_an_interrupted_disposal_is_reported_for_reconciliation(world: World) -> None:
    report = _emit(world)
    original = _original(world, report)
    with tenant_context(world.tenant_id):
        intake = DocumentIntake.objects.get(document=original)
    # A two-copy disposal deleted the final first and stopped: the copy remains.
    assert default_storage.save(intake.quarantine_key, ContentFile(PDF)) == (
        intake.quarantine_key
    )
    with tenant_context(world.tenant_id):
        ContentPurgeClaim.objects.create(
            tenant_id=world.tenant_id,
            client_id=original.client_id,
            content_kind="document",
            content_id=original.pk,
            # In the trigger's own (collation) order, as the purge builds them.
            storage_keys=list(retention_tasks._claim_keys(original)),
            error_code="arquivo_ausente",
        )
    default_storage.delete(original.storage_key)
    lines = _inventory(world)
    line = _one(lines, "INTERRUPTED kind=document", f"id={original.pk}")
    for field in (
        "reason=arquivo_ausente",
        f"missing={original.storage_key} ",
        "action=reconcile",
    ):
        assert field in line
    assert lines[-1] == "INCOMPLETE deleted=0 reasons=disposal-interrupted"
    # Reported, never resolved: the claim stays open and the copy stays stored.
    assert default_storage.exists(intake.quarantine_key)
    with tenant_context(world.tenant_id):
        assert (
            ContentPurgeClaim.objects.get(content_id=original.pk).completed_at is None
        )


def test_a_quarantine_copy_still_stored_is_listed_with_no_deletion_promised(
    world: World,
) -> None:
    report = _emit(world)
    original = _original(world, report)
    with tenant_context(world.tenant_id):
        intake = DocumentIntake.objects.get(document=original)
    # On local storage the scanner removed it; nothing is listed.
    assert not default_storage.exists(intake.quarantine_key)
    assert _with(_inventory(world), "QUARANTINE") == []
    # Non-local storage keeps it after the clean scan.
    default_storage.save(intake.quarantine_key, ContentFile(PDF))
    lines = _inventory(world)
    line = _one(lines, "QUARANTINE", f"intake={intake.pk}")
    assert f"document={original.pk}" in line
    assert "deletion=not-scheduled" in line
    assert _with(lines, "VERSION", f"intake={intake.pk}", "version=current")
    assert lines[-1] == "COMPLETE deleted=0"


def test_a_key_whose_latest_version_is_a_delete_marker_is_not_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listed = [("v2", True, True, T0), ("v1", False, False, T0)]
    monkeypatch.setattr(retention_inventory, "_listed", lambda storage, key: listed)
    assert retention_inventory._stored("k") is False
    listed = [("v1", True, False, T0)]
    assert retention_inventory._stored("k") is True


@pytest.mark.parametrize(
    ("layout", "reason"),
    [
        ("symlink", "armazenamento_com_atalho"),
        ("hard-link", "arquivo_com_vinculos"),
        # Outside its own kind's names before it is outside the root: the purge
        # refuses it first as `chave_fora_do_prefixo`, and so does this listing.
        ("outside-root", "chave_fora_do_prefixo"),
        ("sealed-directory", "armazenamento_nao_inspecionavel"),
    ],
)
def test_a_file_the_purge_cannot_prove_is_listed_with_the_purges_reason(
    world: World, layout: str, reason: str
) -> None:
    report = _emit(world)
    original = _original(world, report)
    root = Path(default_storage.path("")).resolve()
    if layout == "symlink":
        # Anywhere under the root: a second name no key lookup sees.
        (root / "legacy").symlink_to(root / "originals", target_is_directory=True)
    elif layout == "hard-link":
        (root / "spare").mkdir(exist_ok=True)
        (root / "spare" / "copy").hardlink_to(root / original.storage_key)
    elif layout == "outside-root":
        with tenant_context(world.tenant_id):
            DocumentIntake.objects.filter(document=original).update(
                quarantine_key=f"../outside-{original.pk}"
            )
    else:
        # Searchable but not listable: the walk cannot see what is inside.
        (root / "sealed").mkdir()
        (root / "sealed").chmod(0o300)
    try:
        item = _one(_listing(world), "ITEM kind=document", f"id={original.pk}")
    finally:
        if layout == "sealed-directory":
            (root / "sealed").chmod(0o700)
    assert f"refusal={reason}" in item


def test_an_absolute_spelling_of_a_shared_key_is_the_same_key(world: World) -> None:
    report = _emit(world)
    original = _original(world, report)
    root = Path(default_storage.path("")).resolve()
    firm = make_firm("axrm051-inventory-absolute", client_count=1)
    with tenant_context(firm.tenant.pk):
        held = Document.objects.create(
            tenant=firm.tenant,
            client=firm.clients[0],
            storage_key=f"originals/{original.pk}-alheio.pdf",
            sha256=original.sha256,
            original_filename="alheio.pdf",
            content_type="application/pdf",
            byte_size=original.byte_size,
            uploaded_by=firm.owner,
            acquisition_origin=AcquisitionOrigin.UPLOAD_STAFF,
            portal_visible=False,
        )
        # Another tenant's restored copy names the original from the root.
        DocumentIntake.objects.create(
            tenant=firm.tenant,
            client=firm.clients[0],
            document=held,
            state=IntakeState.VERIFIED,
            verdict=ScanVerdict.CLEAN,
            quarantine_key=str(root / original.storage_key),
            scanned_at=timezone.now(),
        )
    item = _one(_inventory(world), "ITEM kind=document", f"id={original.pk}")
    assert "refusal=chave_compartilhada" in item


def test_a_restored_intake_naming_another_kinds_object_is_refused_as_the_purge_does(
    world: World,
) -> None:
    report = _emit(world)
    original = _original(world, report)
    journal = default_storage.save(
        f"provider-journal/{uuid4()}/2026/10/{uuid4()}-1-result.json",
        ContentFile(b"another tenant's receipt"),
    )
    with tenant_context(world.tenant_id):
        DocumentIntake.objects.filter(document=original).update(quarantine_key=journal)
    item = _one(_inventory(world), "ITEM kind=document", f"id={original.pk}")
    assert "refusal=chave_fora_do_prefixo" in item


def test_an_original_no_row_names_is_listed_and_leaves_the_inventory_incomplete(
    world: World,
) -> None:
    _emit(world)
    left = default_storage.save(f"originals/{uuid4()}.pdf", ContentFile(b"left"))
    named = default_storage.save(f"originals/{uuid4()}.pdf", ContentFile(b"named"))
    # Another tenant's row names the second under another spelling of one object.
    firm = make_firm("axrm051-inventory-unnamed", client_count=1)
    with tenant_context(firm.tenant.pk):
        Document.objects.create(
            tenant=firm.tenant,
            client=firm.clients[0],
            storage_key=named.replace("/", "//", 1),
            sha256="0" * 64,
            original_filename="alheio.pdf",
            content_type="application/pdf",
            byte_size=5,
            uploaded_by=firm.owner,
            acquisition_origin=AcquisitionOrigin.UPLOAD_STAFF,
            portal_visible=False,
        )
    lines = _inventory(world)
    assert _with(lines, "UNNAMED", f"key={left}")
    assert not _with(lines, "UNNAMED", f"key={named}")
    assert lines[-1].startswith("INCOMPLETE")
    assert "unnamed-original" in lines[-1]


def test_the_root_is_walked_once_for_the_whole_listing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _original(world, _emit(world))
    _original(world, _emit(world))
    walk = retention._walk_refusal
    calls: list[Path] = []

    def counted(root: Path) -> str:
        calls.append(root)
        return walk(root)

    monkeypatch.setattr(retention, "_walk_refusal", counted)
    lines = _inventory(world)
    assert len(_with(lines, "ITEM kind=document")) >= 2
    assert len(calls) == 1


def test_a_failed_ownership_lookup_keeps_the_item_as_the_purge_does(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _original(world, _emit(world))

    def failing(*_args: object) -> set[str]:
        message = "ownership lookup unavailable"
        raise RuntimeError(message)

    # The purge's own lookup fails, batch and per candidate: every key then
    # counts as another tenant's, and the item is kept (AXRM-051).
    monkeypatch.setattr(retention_tasks, "foreign_storage_keys", failing)
    item = _one(_inventory(world), "ITEM kind=document", f"id={original.pk}")
    assert "refusal=chave_compartilhada" in item


def test_an_open_claim_accounts_for_an_original_whose_keys_are_all_gone(
    world: World,
) -> None:
    original = _original(world, _emit(world))
    with tenant_context(world.tenant_id):
        intake = DocumentIntake.objects.get(document=original)
        ContentPurgeClaim.objects.create(
            tenant_id=world.tenant_id,
            client_id=original.client_id,
            content_kind="document",
            content_id=original.pk,
            # In the trigger's own (collation) order, as the purge builds them.
            storage_keys=list(retention_tasks._claim_keys(original)),
            error_code="arquivo_ausente",
        )
    for key in (original.storage_key, intake.quarantine_key):
        if default_storage.exists(key):
            default_storage.delete(key)
    lines = _inventory(world)
    # The open claim explains the absence: reconciled, never reported as missing.
    assert not _with(lines, "MISSING kind=document", f"id={original.pk}")
    assert _with(lines, "INTERRUPTED kind=document", f"id={original.pk}")
