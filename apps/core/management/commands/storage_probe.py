"""Verify private object storage by write/read/refusal/delete effects."""

import hashlib
import os
import secrets
from typing import Any, Final
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from django.core.files.base import ContentFile
from django.core.files.storage import Storage, default_storage
from django.core.management.base import BaseCommand

PROBE_PREFIX: Final = "_probe/"
PROBE_TOKEN_BYTES: Final = 16
PROBE_PAYLOAD_BYTES: Final = 1024
ANONYMOUS_TIMEOUT_SECONDS: Final = 10
REFUSAL_STATUSES: Final = frozenset({401, 403, 404})


def _direct_object_url(storage: Storage, key: str) -> str:
    """Build the unsigned provider URL without consulting ``storage.url()``."""
    endpoint = urlsplit(str(getattr(storage, "endpoint_url", "")))
    bucket = str(getattr(storage, "bucket_name", ""))
    if endpoint.scheme != "https" or not endpoint.netloc or not bucket:
        msg = "storage endpoint and bucket must form an absolute HTTPS URL"
        raise ValueError(msg)
    if endpoint.query or endpoint.fragment:
        msg = "storage endpoint must not contain a query or fragment"
        raise ValueError(msg)

    encoded_bucket = quote(bucket, safe="")
    encoded_key = quote(key, safe="/")
    path = f"{endpoint.path.rstrip('/')}/{encoded_bucket}/{encoded_key}"
    return urlunsplit((endpoint.scheme, endpoint.netloc, path, "", ""))


def _anonymous_status(url: str) -> int:
    """Return one bounded unauthenticated GET status, including HTTP refusals."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc:
        msg = "anonymous probe URL must be absolute HTTPS"
        raise ValueError(msg)

    request = Request(url, method="GET")  # noqa: S310
    try:
        with urlopen(  # noqa: S310
            request,
            timeout=ANONYMOUS_TIMEOUT_SECONDS,
        ) as response:
            return int(response.status)
    except HTTPError as error:
        return error.code


def _require_refusal(status: int) -> None:
    """Reject success and ambiguous provider responses."""
    if status not in REFUSAL_STATUSES:
        msg = f"anonymous GET returned unexpected status {status}"
        raise RuntimeError(msg)


def _require_same_key(requested_key: str, stored_key: str) -> None:
    """Refuse a backend-selected collision suffix."""
    if stored_key != requested_key:
        msg = "storage changed the requested probe key"
        raise RuntimeError(msg)


def _require_matching_sha(payload: bytes, downloaded: bytes) -> None:
    """Require byte equality through independent SHA-256 digests."""
    if hashlib.sha256(downloaded).digest() != hashlib.sha256(payload).digest():
        msg = "authenticated read SHA-256 mismatch"
        raise RuntimeError(msg)


def _require_absent(storage: Storage, key: str) -> None:
    """Require deletion to be observable through the storage interface."""
    if storage.exists(key):
        msg = "probe object still exists after delete"
        raise RuntimeError(msg)


class Command(BaseCommand):
    """Probe the configured private storage without creating application rows."""

    help = "Verify private object storage by write, read, refusal, and delete effects."

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401, ARG002
        """Run the probe and emit only non-sensitive effect summaries."""
        step = "key-generation"
        cleanup_key: str | None = None
        failed_steps: list[str] = []
        try:
            token = secrets.token_urlsafe(PROBE_TOKEN_BYTES)
            key = f"{PROBE_PREFIX}{token}"
            cleanup_key = key
            self.stdout.write(
                f"KEY_SUFFIX_SHA256 {hashlib.sha256(token.encode()).hexdigest()}",
            )

            step = "write"
            payload = os.urandom(PROBE_PAYLOAD_BYTES)
            stored_key = default_storage.save(key, ContentFile(payload))
            cleanup_key = stored_key
            _require_same_key(key, stored_key)
            self.stdout.write("PASS write")

            step = "authenticated-read"
            with default_storage.open(stored_key, "rb") as stored:
                downloaded = stored.read()
            _require_matching_sha(payload, downloaded)
            self.stdout.write("PASS authenticated-read")

            step = "anonymous-direct-refusal"
            direct_status = _anonymous_status(
                _direct_object_url(default_storage, stored_key),
            )
            _require_refusal(direct_status)
            self.stdout.write(
                "PASS anonymous-direct-refusal: paired with authenticated-read PASS "
                "for the same key",
            )

            step = "storage-url-anonymous-refusal"
            storage_url_status = _anonymous_status(default_storage.url(stored_key))
            _require_refusal(storage_url_status)
            self.stdout.write("PASS storage-url-anonymous-refusal")

            step = "delete-and-confirm-gone"
            default_storage.delete(stored_key)
            _require_absent(default_storage, stored_key)
            self.stdout.write("PASS delete-and-confirm-gone")
        except Exception:  # noqa: BLE001
            failed_steps.append(step)
        finally:
            if cleanup_key is not None:
                try:
                    default_storage.delete(cleanup_key)  # PILOT-203-FINALLY-DELETE
                except Exception:  # noqa: BLE001
                    failed_steps.append("finally-cleanup")

        if failed_steps:
            for failed_step in failed_steps:
                self.stdout.write(f"FAIL step={failed_step}")
            raise SystemExit(1)
