"""Malware-scan port and the quarantine verdict service (S8.3, S7.5, M5).

`Scanner.scan(stream) -> Verdict` is the port. `ClamdScanner` speaks clamd's INSTREAM
protocol over TCP (`CLAMD_HOST:CLAMD_PORT`); `FakeScanner` is the test double (the
EICAR string is infected, a `down` scanner is unavailable). `CLAMAV_ENABLED=false`
yields no scanner at all: the file goes to `verificacao_indisponivel`, which staff may
download only behind a warning and the portal never re-serves (OD-11, OG-05).

One scan is three phases, and the network I/O never runs inside a transaction (S7.6):

- A `claim_scan` reads the intake and where its bytes are, in `tenant_context`;
- B the bytes stream to the scanner with no transaction open (`assert_no_transaction`),
  and a clean file is copied from its quarantine key to its final key;
- C `record_verdict` / `record_failure` lock the intake row, refuse a stale ticket
  (the `version` moved: another scan already decided) without writing anything, and
  record the verdict with one cycle event per linked cycle.

Only after C commits is the quarantine copy of a clean file deleted (copy + delete).
A rejected original is never deleted: it stays under `quarantine/` and is never
served. A scanner that stays down is retried with backoff until 24 h after the
upload, then the intake becomes `verificacao_indisponivel` and a
`verificacao_indisponivel` exception opens on every linked cycle.
"""

import hashlib
import socket
import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final, Protocol
from uuid import UUID

import sentry_sdk
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import Storage, default_storage
from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import User
from apps.core.tasks import assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.cycles.evidence import (
    PENDING_SCAN_STATES,
    DocumentIntake,
    EvidenceLink,
    IntakeState,
    ScanVerdict,
)
from apps.cycles.records import CycleEvent, ServiceCycle
from apps.cycles.services.exceptions import open_exception

EICAR: Final = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
EXCEPTION_REASON: Final = "verificacao_indisponivel"
RETRY_DEADLINE: Final = timedelta(hours=24)
BACKOFF_MINUTES: Final = (1, 5, 15, 30, 60)
REPLY_MAX_BYTES: Final = 4096
CHUNK_BYTES: Final = 64 * 1024
DISABLED_REASON: Final = "clamav_desativado"
DEADLINE_REASON: Final = "scanner_indisponivel_24h"


class ByteStream(Protocol):
    """Anything with a byte `read`: a storage `File`, `BytesIO`, a socket file."""

    def read(self, size: int = -1, /) -> bytes:
        """Return up to `size` bytes, or everything when negative."""
        ...


class ScannerUnavailable(Exception):  # noqa: N818 - domain vocabulary
    """The scanner could not give a verdict; the file stays quarantined."""


@dataclass(frozen=True, slots=True)
class Verdict:
    """One scanner answer. `signature` names the threat when infected."""

    infected: bool
    signature: str
    engine: str
    signature_version: str


class Scanner(Protocol):
    """The port: stream the bytes, get a verdict or `ScannerUnavailable`."""

    def scan(self, stream: ByteStream) -> Verdict:
        """Scan the stream to its end."""
        ...


@dataclass(frozen=True, slots=True)
class ClamdScanner:
    """clamd INSTREAM over TCP: chunked, size-capped, every socket op timed out."""

    host: str
    port: int = 3310
    timeout: float = 30.0
    chunk_bytes: int = CHUNK_BYTES
    max_bytes: int = 10 * 1024 * 1024

    def _command(self, command: bytes, body: ByteStream | None = None) -> str:
        try:
            with socket.create_connection(
                (self.host, self.port), timeout=self.timeout
            ) as sock:
                sock.settimeout(self.timeout)
                sock.sendall(b"z" + command + b"\0")
                if body is not None:
                    self._stream(sock, body)
                return self._reply(sock)
        except OSError as error:
            msg = f"clamd {command.decode()} failed: {type(error).__name__}"
            raise ScannerUnavailable(msg) from error

    def _stream(self, sock: socket.socket, body: ByteStream) -> None:
        total = 0
        while chunk := body.read(self.chunk_bytes):
            total += len(chunk)
            if total > self.max_bytes:
                msg = "stream exceeds the scanner size limit"
                raise ScannerUnavailable(msg)
            sock.sendall(struct.pack("!L", len(chunk)) + chunk)
        sock.sendall(struct.pack("!L", 0))

    @staticmethod
    def _reply(sock: socket.socket) -> str:
        data = b""
        while b"\0" not in data and len(data) < REPLY_MAX_BYTES:
            part = sock.recv(REPLY_MAX_BYTES)
            if not part:
                break
            data += part
        return data.split(b"\0", 1)[0].decode("utf-8", "replace").strip()

    def version(self) -> tuple[str, str]:
        """`ClamAV 1.4.1/27421/<date>` -> (engine, signature database version)."""
        reply = self._command(b"VERSION")
        engine, _, signatures = reply.partition("/")
        return engine[:64], signatures[:128]

    def scan(self, stream: ByteStream) -> Verdict:
        """INSTREAM the bytes; `stream: OK` is clean, `stream: X FOUND` infected."""
        assert_no_transaction()
        engine, signatures = self.version()
        reply = self._command(b"INSTREAM", stream)
        _, _, result = reply.partition(": ")
        if result == "OK":
            return Verdict(
                infected=False,
                signature="",
                engine=engine,
                signature_version=signatures,
            )
        if result.endswith(" FOUND"):
            return Verdict(
                infected=True,
                signature=result.removesuffix(" FOUND")[:48],
                engine=engine,
                signature_version=signatures,
            )
        msg = "clamd returned no verdict"
        raise ScannerUnavailable(msg)


@dataclass(slots=True)
class FakeScanner:
    """Test double: EICAR is infected; `down=True` is an unreachable scanner."""

    down: bool = False
    scanned: list[int] = field(default_factory=list)

    def scan(self, stream: ByteStream) -> Verdict:
        """Read everything, like clamd would, and look for the EICAR string."""
        assert_no_transaction()
        if self.down:
            msg = "fake scanner down"
            raise ScannerUnavailable(msg)
        payload = stream.read()
        self.scanned.append(len(payload))
        if EICAR in payload:
            return Verdict(
                infected=True,
                signature="Eicar-Test-Signature",
                engine="fake",
                signature_version="test",
            )
        return Verdict(
            infected=False, signature="", engine="fake", signature_version="test"
        )


def scanner_from_settings() -> Scanner | None:
    """Build the configured scanner, or None while `CLAMAV_ENABLED` is off."""
    if not settings.CLAMAV_ENABLED:
        return None
    return ClamdScanner(
        settings.CLAMD_HOST,
        settings.CLAMD_PORT,
        timeout=settings.CLAMD_TIMEOUT_SECONDS,
        max_bytes=settings.PORTAL_UPLOAD_MAX_BYTES,
    )


class ScanOutcome(StrEnum):
    """What one scan attempt did."""

    VERIFIED = "verificado"
    INFECTED = "rejeitado_malware"
    RETRY = "retry"
    UNAVAILABLE = "verificacao_indisponivel"
    STALE = "stale"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class ScanTicket:
    """Phase A's snapshot: which row, at which version, and where its bytes are."""

    tenant_id: UUID
    intake_id: UUID
    version: int
    object_key: str
    storage_key: str
    sha256: str

    @property
    def quarantined(self) -> bool:
        """True while the bytes still sit under the quarantine prefix."""
        return self.object_key != self.storage_key


def claim_scan(tenant_id: UUID, intake_id: UUID) -> ScanTicket | None:
    """Phase A: read the pending intake inside the tenant's context, or None."""
    with tenant_context(tenant_id):
        intake = (
            DocumentIntake.objects.select_related("document")
            .filter(pk=intake_id, state__in=PENDING_SCAN_STATES)
            .first()
        )
        if intake is None:
            return None
        return ScanTicket(
            tenant_id=tenant_id,
            intake_id=intake.pk,
            version=intake.version,
            object_key=intake.object_key,
            storage_key=intake.document.storage_key,
            sha256=intake.document.sha256,
        )


def run_scan(ticket: ScanTicket, scanner: Scanner, storage: Storage) -> Verdict:
    """Phase B: stream the quarantined bytes to the scanner, outside any transaction."""
    assert_no_transaction()
    try:
        with storage.open(ticket.object_key, "rb") as handle:
            return scanner.scan(handle)
    except OSError as error:
        msg = "quarantined object unreadable"
        raise ScannerUnavailable(msg) from error


class IntegrityMismatch(Exception):  # noqa: N818 - domain vocabulary
    """The stored bytes no longer hash to the document's sha256."""


def _read_verified(storage: Storage, key: str, sha256: str) -> bytes:
    with storage.open(key, "rb") as handle:
        payload: bytes = handle.read()
    if hashlib.sha256(payload).hexdigest() != sha256:
        msg = f"sha256 mismatch at {key}"
        raise IntegrityMismatch(msg)
    return payload


def promote(ticket: ScanTicket, storage: Storage) -> None:
    """Phase B, clean file: copy the quarantined bytes to the final key (no delete).

    Idempotent across a crash between the copy and phase C: an object already at the
    final key is accepted only if it hashes to the document's sha256.
    """
    assert_no_transaction()
    if not ticket.quarantined:
        return
    try:
        if storage.exists(ticket.storage_key):
            _read_verified(storage, ticket.storage_key, ticket.sha256)
            return
        payload = _read_verified(storage, ticket.object_key, ticket.sha256)
        saved = storage.save(ticket.storage_key, ContentFile(payload))
    except OSError as error:
        msg = "promotion copy failed"
        raise ScannerUnavailable(msg) from error
    if saved != ticket.storage_key:
        _discard(storage, saved)
        msg = "storage changed the requested document key"
        raise RuntimeError(msg)


def _discard(storage: Storage, key: str) -> None:
    try:
        storage.delete(key)  # PILOT-205-COMPENSATING-DELETE
    except Exception as error:  # noqa: BLE001 - reported; the orphan report lists it
        sentry_sdk.capture_exception(error)


def _locked(ticket: ScanTicket) -> DocumentIntake | None:
    """Lock the row; None when the ticket is stale (and nothing is written)."""
    intake = (
        DocumentIntake.objects.select_for_update()
        .select_related("document")
        .get(pk=ticket.intake_id)
    )
    if intake.version != ticket.version or intake.state not in PENDING_SCAN_STATES:
        return None
    return intake


def _linked_cycles(intake: DocumentIntake) -> list[tuple[ServiceCycle, UUID]]:
    """Each cycle a link puts this document in, locked, with the request's author."""
    authors: dict[UUID, UUID] = {}
    for cycle_id, author_id in (
        EvidenceLink.objects.filter(document_id=intake.document_id)
        .order_by("created_at", "pk")
        .values_list("request__cycle_id", "request__created_by_id")
    ):
        authors.setdefault(cycle_id, author_id)
    cycles = ServiceCycle.objects.select_for_update().filter(pk__in=list(authors))
    return [(cycle, authors[cycle.pk]) for cycle in cycles.order_by("pk")]


def _save(intake: DocumentIntake, now: datetime) -> list[tuple[ServiceCycle, UUID]]:
    intake.version += 1
    intake.save(
        update_fields=[
            "state",
            "reason",
            "verdict",
            "scan_engine",
            "scan_signature_version",
            "scanned_at",
            "scan_attempts",
            "next_scan_at",
            "version",
        ]
    )
    cycles = _linked_cycles(intake)
    for cycle, _author in cycles:
        CycleEvent.objects.create(
            tenant_id=cycle.tenant_id,
            client_id=cycle.client_id,
            cycle=cycle,
            actor=None,
            type="intake.scanned",
            aggregate_type="DocumentIntake",
            aggregate_id=intake.pk,
            aggregate_version=intake.version,
            occurred_at=now,
            payload={
                "document_id": str(intake.document_id),
                "state": intake.state,
                "verdict": intake.verdict,
                "attempts": intake.scan_attempts,
            },
        )
    return cycles


def record_verdict(
    ticket: ScanTicket, verdict: Verdict | None, *, now: datetime
) -> ScanOutcome:
    """Phase C: record a verdict, or the disabled switch when `verdict` is None."""
    with tenant_context(ticket.tenant_id):
        intake = _locked(ticket)
        if intake is None:
            return ScanOutcome.STALE
        intake.next_scan_at = None
        if verdict is None:
            intake.state = IntakeState.SCAN_UNAVAILABLE
            intake.verdict = ScanVerdict.UNAVAILABLE
            intake.reason = DISABLED_REASON
            outcome = ScanOutcome.UNAVAILABLE
        else:
            intake.scan_engine = verdict.engine
            intake.scan_signature_version = verdict.signature_version
            intake.scanned_at = now
            intake.scan_attempts += 1
            if verdict.infected:
                intake.state = IntakeState.REJECTED_MALWARE
                intake.verdict = ScanVerdict.INFECTED
                intake.reason = f"malware:{verdict.signature}"[:64]
                outcome = ScanOutcome.INFECTED
            else:
                intake.state = IntakeState.VERIFIED
                intake.verdict = ScanVerdict.CLEAN
                intake.reason = ""
                outcome = ScanOutcome.VERIFIED
        _save(intake, now)
        return outcome


def backoff(attempts: int) -> timedelta:
    """1, 5, 15, 30 then 60 minutes between attempts."""
    return timedelta(minutes=BACKOFF_MINUTES[min(attempts, len(BACKOFF_MINUTES)) - 1])


def record_failure(ticket: ScanTicket, *, now: datetime) -> ScanOutcome:
    """Phase C, scanner down: schedule a retry, or give up 24 h after the upload."""
    with tenant_context(ticket.tenant_id):
        intake = _locked(ticket)
        if intake is None:
            return ScanOutcome.STALE
        intake.scan_attempts += 1
        if now - intake.created_at < RETRY_DEADLINE:
            intake.next_scan_at = now + backoff(intake.scan_attempts)
            _save(intake, now)
            return ScanOutcome.RETRY
        intake.state = IntakeState.SCAN_UNAVAILABLE
        intake.verdict = ScanVerdict.UNAVAILABLE
        intake.reason = DEADLINE_REASON
        intake.next_scan_at = None
        for cycle, author_id in _save(intake, now):
            open_exception(
                EXCEPTION_REASON,
                intake.pk,
                actor=None,
                cycle_id=cycle.pk,
                expected_version=cycle.version,
                owner=cycle.owner or User.objects.get(pk=author_id),
                review_on=timezone.localdate(now),
                evidence_refs=(intake.document_id,),
            )
        return ScanOutcome.UNAVAILABLE


def scan_intake(
    tenant_id: UUID,
    intake_id: UUID,
    *,
    scanner: Scanner | None,
    now: datetime,
    storage: Storage = default_storage,
) -> ScanOutcome:
    """Run phases A, B and C for one intake; `now` is injected for the deadline."""
    assert_no_transaction()
    ticket = claim_scan(tenant_id, intake_id)
    if ticket is None:
        return ScanOutcome.SKIPPED
    if scanner is None:
        return record_verdict(ticket, None, now=now)
    try:
        verdict = run_scan(ticket, scanner, storage)
        if not verdict.infected:
            promote(ticket, storage)
    except ScannerUnavailable:
        return record_failure(ticket, now=now)
    outcome = record_verdict(ticket, verdict, now=now)
    if outcome is ScanOutcome.VERIFIED and ticket.quarantined:
        _discard(storage, ticket.object_key)
    return outcome


def due_intakes(tenant_id: UUID, now: datetime, limit: int = 50) -> list[UUID]:
    """Pending intakes whose retry time has come (or that were never tried)."""
    with tenant_context(tenant_id):
        return list(
            DocumentIntake.objects.filter(state__in=PENDING_SCAN_STATES)
            .filter(Q(next_scan_at__isnull=True) | Q(next_scan_at__lte=now))
            .order_by("created_at", "pk")
            .values_list("pk", flat=True)[:limit]
        )
