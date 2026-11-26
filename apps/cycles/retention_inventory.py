"""The operator's read-only retention inventory (AXRM-051, A11, OG-R13).

It reads the purge job's own candidates, refusals and holds and changes nothing.
For each subject it prints the client, the references that date it (or why none
does), the refusal that keeps it, every stored version of each copy with the
expiry that applies to it, and the state of its claim; then completed disposals
with what storage still holds, rows whose object is missing without a disposal,
federal records by field class, authority evidence and the provider journal. It
prints identifiers, dates, hashes and storage keys only: never a name, CNPJ,
amount, file name or free text.

A noncurrent version is dated only from a lifecycle rule read back from the
bucket and understood here; an unreadable or unfamiliar rule is reported as
such, and the inventory then ends INCOMPLETE rather than COMPLETE.

It also lists, without resolving them, what the purge leaves behind: a refusal
for a key another tenant's row names, a disposal claim that removed some of its
keys and stopped (to reconcile; the inventory is INCOMPLETE until then), and a
verified intake's quarantine copy still stored, which no date is promised for;
and an object under `originals/` that no row of any tenant names (the inventory
is then INCOMPLETE).
"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final
from uuid import UUID

from botocore.exceptions import ClientError
from django.core.exceptions import SuspiciousOperation
from django.core.files.storage import Storage, default_storage
from django.utils import timezone
from storages.backends.s3 import S3Storage

from apps.core.tasks import assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.cycles.evidence import CLEAN_STATES, DocumentIntake
from apps.cycles.retention import (
    _remote_name,
    keys_named_by_rows,
    local_root_refusal,
    ownership_refusal,
    retention_decision,
    single_copy_storage,
)
from apps.cycles.retention_records import ContentPurgeClaim, LegalHold
from apps.cycles.retention_tasks import (
    Candidate,
    _candidates,
    _foreign_for,
    _foreign_or_none,
    disposal_refusal,
)
from apps.integrations import provider_journal
from apps.integrations.models import AuthorityRecord, FirmAuthorization
from apps.integrations.serpro.models import (
    ACTIVE_QUERY_STATES,
    PaymentPage,
    PaymentQuery,
)
from apps.obligations.models import (
    FederalConsultation,
    FederalPayment,
    FiscalSituationReport,
)
from apps.tenants.models import Tenant

# How each federal record's fields are kept while OG-08 decides their periods.
# Identifiers, dates, hashes and barriers prove what happened; the other classes
# are kept unchanged pending OG-08 and are never printed.
FIELD_CLASSES: Final[Mapping[str, str]] = {
    "federal_consultation": "identifiers,dates,outcome",
    "federal_payment": "identifiers,dates;amounts=pending-og08",
    "fiscal_situation_report": "identifiers,dates,hashes,call-barrier",
    "payment_query": "identifiers,dates,state",
}


@dataclass(frozen=True, slots=True)
class Lifecycle:
    """What the bucket's noncurrent-version expiry is known to be.

    `state` is `single-copy` (a delete removes the only copy), `verified` (rules
    read back and understood), `unverified` (no rule could be read back) or
    `unknown` (a rule this inventory does not interpret).
    """

    state: str
    rules: tuple[tuple[str, int], ...] = ()


def _s3(storage: S3Storage) -> tuple[Any, str]:
    """Return the backend's boto3 client and bucket (untyped in django-storages)."""
    return getattr(storage, "connection").meta.client, str(  # noqa: B009
        getattr(storage, "bucket_name")  # noqa: B009
    )


def lifecycle(storage: Storage) -> Lifecycle:
    """Read back the bucket's noncurrent-version rules, never inferring absence."""
    if single_copy_storage(storage):
        return Lifecycle("single-copy")
    if not isinstance(storage, S3Storage):
        return Lifecycle("unknown")
    client, bucket = _s3(storage)
    try:
        config = client.get_bucket_lifecycle_configuration(Bucket=bucket)
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code")
        # An S3-compatible endpoint may answer this while a native rule exists.
        return Lifecycle(
            "unverified" if code == "NoSuchLifecycleConfiguration" else "unknown"
        )
    rules: list[tuple[str, int]] = []
    for rule in config.get("Rules", []):
        if rule.get("Status") != "Enabled":
            continue
        expiry = rule.get("NoncurrentVersionExpiration", {})
        selector = rule.get("Filter", {"Prefix": rule.get("Prefix", "")})
        if "NewerNoncurrentVersions" in expiry or set(selector) - {"Prefix"}:
            return Lifecycle("unknown")
        if expiry.get("NoncurrentDays"):
            rules.append(
                (str(selector.get("Prefix", "")), int(expiry["NoncurrentDays"]))
            )
    return Lifecycle("verified", tuple(rules))


@dataclass(frozen=True, slots=True)
class Version:
    """One stored version of a key and the expiry that applies to it."""

    version: str
    latest: bool
    delete_marker: bool
    modified_at: datetime
    expires: str


def _when(value: datetime | None) -> str:
    return "-" if value is None else timezone.localtime(value).isoformat()


def _listed(storage: Storage, key: str) -> list[tuple[str, bool, bool, datetime]]:
    """(version, latest, delete marker, modified) for exactly `key`, newest first."""
    if not isinstance(storage, S3Storage):
        if not storage.exists(key):
            return []
        return [("current", True, False, storage.get_modified_time(key))]
    name = _remote_name(key, storage)  # the bucket key the backend itself resolves
    client, bucket = _s3(storage)
    paginator = client.get_paginator("list_object_versions")
    found: list[tuple[str, bool, bool, datetime]] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=name):
        for field, marker in (("Versions", False), ("DeleteMarkers", True)):
            found += [
                (entry["VersionId"], entry["IsLatest"], marker, entry["LastModified"])
                for entry in page.get(field, [])
                if entry["Key"] == name  # a sibling key sharing the prefix is not it
            ]
    return sorted(found, key=lambda entry: (entry[3], entry[1]), reverse=True)


def versions(
    storage: Storage, key: str, rules: Lifecycle, current_expiry: str
) -> list[Version]:
    """Every version of `key`, each with its fiscal expiry or lifecycle eligibility."""
    days = min((d for prefix, d in rules.rules if key.startswith(prefix)), default=None)
    listed = _listed(storage, key)
    found: list[Version] = []
    for index, (version, latest, marker, modified) in enumerate(listed):
        if latest and not marker:
            expires = current_expiry
        elif marker:
            expires = "-"
        elif rules.state != "verified":
            expires = f"lifecycle-{rules.state}"
        elif days is None or index == 0:
            expires = "no-rule"
        else:
            # Eligible once noncurrent for `days`: not a deletion date.
            eligible = listed[index - 1][3] + timedelta(days=days)
            expires = f"eligible-from:{_when(eligible)}"
        found.append(Version(version, latest, marker, modified, expires))
    return found


def _version_lines(
    head: str, keys: tuple[str, ...], rules: Lifecycle, expiry: str
) -> Iterator[str]:
    for key in keys:
        try:
            listed = versions(default_storage, key, rules, expiry)
        except SuspiciousOperation:
            # A key the backend refuses to address names nothing it can list: it is
            # reported as such, and the listing goes on (AXRM-051).
            yield f"VERSION {head} key={key} version=unaddressable"
            continue
        if not listed:
            yield f"VERSION {head} key={key} version=none"
        for item in listed:
            yield (
                f"VERSION {head} key={key} version={item.version} "
                f"latest={'yes' if item.latest else 'no'} "
                f"delete_marker={'yes' if item.delete_marker else 'no'} "
                f"modified={_when(item.modified_at)} expires={item.expires}"
            )


@dataclass(frozen=True, slots=True)
class _Snapshot:
    candidates: list[tuple[Candidate, str, str, str]]
    disposed: list[ContentPurgeClaim]
    open_claims: list[ContentPurgeClaim]
    copies: list[tuple[UUID, UUID, UUID, str]]
    holds: list[LegalHold]
    records: list[str]
    authority: list[str]


def _snapshot(tenant_id: UUID, client_id: UUID | None, now: datetime) -> _Snapshot:
    scope: dict[str, Any] = {} if client_id is None else {"client_id": client_id}
    with tenant_context(tenant_id):
        claims = {
            (claim.content_kind, claim.content_id): claim
            for claim in ContentPurgeClaim.objects.filter(**scope)
        }
        candidates = []
        for candidate in _candidates():
            if client_id is not None and candidate.client_id != client_id:
                continue
            decision = retention_decision(
                candidate.client_id, candidate.expires_at, now=now
            ).reason
            claim = claims.get((candidate.kind, candidate.id))
            state = "none" if claim is None else f"open:{claim.error_code or '-'}"
            candidates.append(
                (candidate, decision, disposal_refusal(candidate) or "-", state)
            )
        disposed = [c for c in claims.values() if c.completed_at is not None]
        open_claims = [c for c in claims.values() if c.completed_at is None]
        # Verified intakes whose quarantine copy may still be stored (kept on
        # non-local storage, or for a hold or another row): listed, never promised.
        copies = list(
            DocumentIntake.objects.filter(state__in=CLEAN_STATES, **scope)
            .exclude(quarantine_key="")
            .values_list("pk", "document_id", "client_id", "quarantine_key")
        )
        holds = list(LegalHold.objects.filter(released_at=None, **scope))
        records = [
            f"RECORD kind={kind} rows={model.objects.filter(**scope).count()} "
            f"fields={FIELD_CLASSES[kind]} disposition=kept-pending-og08"
            for kind, model in (
                ("federal_consultation", FederalConsultation),
                ("federal_payment", FederalPayment),
                ("fiscal_situation_report", FiscalSituationReport),
                ("payment_query", PaymentQuery),
            )
        ]
        records += [
            f"RECORD kind=payment_page id={page.pk} client={page.client_id} "
            f"condition={_page_condition(page)}"
            for page in PaymentPage.objects.filter(**scope).select_related("query")
        ]
        authority = [
            f"AUTHORITY kind=authority_record id={row.pk} client={row.client_id} "
            f"evidence={row.evidence_document_id} disposition=protected"
            for row in AuthorityRecord.objects.filter(
                evidence_document__isnull=False, **scope
            )
        ]
        if client_id is None:  # firm-level evidence is never shown as a client's
            authority += [
                f"AUTHORITY kind=firm_authorization id={row.pk} "
                f"evidence={row.signed_document_id} disposition=protected"
                for row in FirmAuthorization.objects.filter(
                    signed_document__isnull=False
                )
            ]
    return _Snapshot(
        candidates, disposed, open_claims, copies, holds, records, authority
    )


def _page_condition(page: PaymentPage) -> str:
    """Say when a working page goes: when its query ends, or by its TTL ceiling."""
    if page.query.state not in ACTIVE_QUERY_STATES:
        return "removed-on-next-sweep"
    return f"ttl-ceiling:{_when(page.created_at + timedelta(days=7))}"


def _stored(key: str) -> bool:
    """Whether a current, non-deleted version of `key` exists."""
    return any(
        latest and not marker
        for _version, latest, marker, _modified in _listed(default_storage, key)
    )


def _unfinished_lines(
    snap: _Snapshot, rules: Lifecycle, incomplete: set[str]
) -> list[str]:
    """Open claims missing some keys, and quarantine copies still stored."""
    lines: list[str] = []
    for claim in snap.open_claims:
        missing = [key for key in claim.storage_keys if not _stored(key)]
        if missing:
            # A disposal that removed some of its keys and stopped (a final-first
            # delete interrupted, say): refused and never completed, so it is
            # reported for the operator to reconcile, never resolved here.
            head = f"kind={claim.content_kind} id={claim.content_id}"
            lines.append(
                f"INTERRUPTED {head} client={claim.client_id} "
                f"reason={claim.error_code or '-'} missing={','.join(missing)} "
                f"action=reconcile"
            )
            incomplete.add("disposal-interrupted")
    for intake_id, document_id, owner, key in snap.copies:
        copy = list(_version_lines(f"intake={intake_id}", (key,), rules, "-"))
        if all(line.endswith("version=none") for line in copy):
            continue
        lines.append(
            f"QUARANTINE intake={intake_id} document={document_id} client={owner} "
            f"key={key} deletion=not-scheduled"
        )
        lines += copy
    return lines


def _purge_refusal(
    tenant_id: UUID, candidate: Candidate, own: str, foreign: set[str], root: str | None
) -> str:
    """Return the refusal the purge would record for this candidate, or `-`.

    The purge's own steps, in its order: the candidate's refusal (its kind's
    names first); a key another tenant names; then, for stored content, the
    ownership check the purge runs under the key locks, judged against the one
    walk of the root this listing takes (AXRM-051).
    """
    if own != "-":
        return own
    if foreign.intersection(candidate.keys):
        return "chave_compartilhada"
    if not candidate.keys:
        return "-"  # nothing stored: the purge decides no ownership either
    document = candidate.kind == "document"
    refusal = ownership_refusal(
        tenant_id,
        candidate.keys,
        document_id=candidate.id if document else None,
        export_id=None if document else candidate.id,
        storage=default_storage,
        root_refusal=root,
    )
    return refusal or "-"


def _unnamed_originals() -> list[str]:
    """Return the objects under `originals/` that no row of any tenant names.

    A remote promotion writes a fresh `originals/<uuid>` key; one whose save was
    refused and whose cleanup failed is left there. Its owner is unknown, so it is
    listed for every tenant-wide inventory and never resolved here (AXRM-051).
    """
    try:
        _directories, files = default_storage.listdir("originals")
    except FileNotFoundError:
        return []
    keys = [f"originals/{name}" for name in files]
    # PLATFORM_QUERY_OK: storage keys are global, so every tenant's rows are read.
    tenants = Tenant.objects.values_list("pk", flat=True)
    return sorted(set(keys) - keys_named_by_rows(tenants, keys, default_storage))


def inventory_lines(
    tenant_id: UUID, *, now: datetime, client_id: UUID | None = None
) -> list[str]:
    """Return the read-only listing for one tenant, or one of its clients."""
    assert_no_transaction()
    snap = _snapshot(tenant_id, client_id, now)
    # The purge keeps a key a row of another tenant names, under any spelling that
    # resolves to the same object (AXRM-051); so does this, with the purge's reason.
    batch = _foreign_or_none(
        tenant_id, [key for candidate, *_ in snap.candidates for key in candidate.keys]
    )
    rules = lifecycle(default_storage)
    root = local_root_refusal()  # one walk of the root for the whole listing
    lines = [
        (
            f"INVENTORY tenant={tenant_id} client={client_id or '-'} mode=read-only "
            f"now={_when(now)} lifecycle={rules.state}"
        )
    ]
    lines += [f"HOLD client={hold.client_id} hold={hold.pk}" for hold in snap.holds]
    incomplete: set[str] = (
        set()
        if rules.state in {"single-copy", "verified"}
        else {f"lifecycle-{rules.state}"}
    )
    for candidate, decision, own, claim_state in snap.candidates:
        foreign = _foreign_for(tenant_id, batch, candidate)
        refusal = _purge_refusal(tenant_id, candidate, own, foreign, root)
        head = f"kind={candidate.kind} id={candidate.id}"
        anchors = ",".join(
            f"{a.basis}:{a.on.isoformat()}:{a.source_id}" for a in candidate.anchors
        )
        lines.append(
            f"ITEM {head} client={candidate.client_id} anchors={anchors or '-'} "
            f"gap={candidate.gap or '-'} expires={_when(candidate.expires_at)} "
            f"decision={decision} refusal={refusal} claim={claim_state}"
        )
        expiry = _when(candidate.expires_at)
        listed = list(_version_lines(head, candidate.keys, rules, expiry))
        lines += listed
        if (
            candidate.kind == "document"
            and claim_state == "none"
            and all(line.endswith("version=none") for line in listed)
        ):
            # The row says the original exists; no disposal accounts for its absence.
            lines.append(f"MISSING {head} proof=none")
            incomplete.add("missing-without-proof")
    for claim in snap.disposed:
        head = f"kind={claim.content_kind} id={claim.content_id}"
        held = list(_version_lines(head, tuple(claim.storage_keys), rules, "-"))
        present = [line for line in held if not line.endswith("version=none")]
        lines.append(
            f"DISPOSED {head} client={claim.client_id} "
            f"completed={_when(claim.completed_at)} "
            f"disposition={'unproved' if present else 'confirmed'}"
        )
        lines += held
        if present:
            incomplete.add("disposition-unproved")
    lines += _unfinished_lines(snap, rules, incomplete)
    lines += snap.records + snap.authority
    if client_id is None:
        client_keys, firm_keys = provider_journal.object_keys(tenant_id)
        for scope, keys in (("client", client_keys), ("firm", firm_keys)):
            for key in keys:
                lines.append(f"JOURNAL scope={scope} key={key} retention=journal-5y")
                lines += _version_lines(f"journal={scope}", (key,), rules, "journal-5y")
        for key in _unnamed_originals():
            lines.append(f"UNNAMED key={key} owner=unknown deletion=not-scheduled")
            lines += _version_lines("unnamed", (key,), rules, "-")
            incomplete.add("unnamed-original")
    else:
        lines.append("JOURNAL scope=tenant omitted-for-client")
    lines.append(
        "COMPLETE deleted=0"
        if not incomplete
        else f"INCOMPLETE deleted=0 reasons={','.join(sorted(incomplete))}"
    )
    return lines
