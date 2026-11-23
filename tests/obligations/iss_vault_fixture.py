"""Synthetic vault failures used by the ISS registration and recovery tests."""

from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError, ReadTimeoutError
from django.core.files.storage import default_storage
from s3transfer.exceptions import (
    RetriesExceededError,
    S3DownloadFailedError,
    S3ValidationError,
)

from apps.cycles.parsing import MALFORMED, UnsafeDocument
from apps.obligations import iss_retention_evidence_v1
from apps.obligations.iss_retention_v1 import corroborated_iss_code

ISS_VAULT_FAULTS = (
    "503",
    "timeout",
    "missing",
    "digest",
    "parse",
    "timeout-stream",
    "digest-etag",
    "digest-range",
)


@contextmanager
def fault_iss_original(fault: str, key: str, payload: bytes) -> Iterator[None]:
    storage_open = default_storage.open

    def open_fault(name: str, mode: str = "rb") -> object:
        if name == key and mode == "rb":
            if fault == "503":
                raise ClientError(
                    {
                        "Error": {"Code": "SlowDown"},
                        "ResponseMetadata": {
                            "HTTPStatusCode": 503,
                            "RequestId": "test-request",
                            "HostId": "test-host",
                            "HTTPHeaders": {},
                            "RetryAttempts": 1,
                        },
                    },
                    "GetObject",
                )
            if fault == "timeout":
                raise ReadTimeoutError(endpoint_url="https://example.invalid")
            if fault == "missing":
                raise FileNotFoundError
            if fault == "digest":
                return BytesIO(payload.replace(b"12.34", b"42.34"))
            if fault in ("timeout-stream", "digest-etag", "digest-range"):
                # Managed downloads may fail on stream.read(), after open succeeds.
                stream = MagicMock(spec=BytesIO)
                stream.__enter__.return_value.read.side_effect = {
                    "timeout-stream": RetriesExceededError(
                        ReadTimeoutError(endpoint_url="https://example.invalid")
                    ),
                    "digest-etag": S3DownloadFailedError(),
                    "digest-range": S3ValidationError(),
                }[fault]
                return stream
        return storage_open(name, mode)

    def parse_fault(raw: bytes, code: str | None) -> str | None:
        if fault == "parse" and raw == payload:
            raise UnsafeDocument(MALFORMED)
        return corroborated_iss_code(raw, code)

    with (
        patch.object(default_storage, "open", side_effect=open_fault),
        patch.object(
            iss_retention_evidence_v1, "corroborated_iss_code", side_effect=parse_fault
        ),
    ):
        yield
