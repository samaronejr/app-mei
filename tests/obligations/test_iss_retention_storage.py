"""Exercise the real vault adapter with synthetic HTTP responses."""

from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from io import BytesIO
from secrets import token_hex
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from botocore.awsrequest import AWSPreparedRequest, AWSResponse
from botocore.compat import HTTPHeaders
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, ConnectTimeoutError
from storages.backends.s3 import S3Storage

from apps.obligations import iss_retention_evidence_v1 as evidence
from tests.obligations.fiscal_fixtures import nfse


class WireBody(BytesIO):
    def stream(self) -> Iterator[bytes]:
        yield self.read()


class BrokenBody(WireBody):
    def __init__(self, payload: bytes, *, prefix: int = 0) -> None:
        super().__init__(payload)
        self.prefix = prefix
        self.consumed = 0
        self.attempted = False

    def read(self, size: int | None = -1, /) -> bytes:
        assert size is not None
        assert size > 0
        self.attempted = True
        self.consumed += len(super().read(self.prefix))
        if self.prefix:
            message = "synthetic connection reset after receiving bytes"
            raise ConnectionResetError(message)
        message = "synthetic body decoder failure"
        raise ValueError(message)


class ISSVault:
    """Keep the real S3Storage, botocore parser, and stream; intercept only HTTP."""

    def __init__(
        self,
        payload: bytes,
        *,
        status: int = 200,
        content_range: str | None = None,
        body: bytes | WireBody | None = None,
        content_length: int | None = None,
    ) -> None:
        self.storage = S3Storage(
            bucket_name="private-vault",
            location="retained",
            endpoint_url="https://vault.invalid",
            access_key="synthetic",
            secret_key=token_hex(16),
            region_name="us-east-1",
            client_config=Config(
                s3={"addressing_style": "path"},
                response_checksum_validation="when_required",
                retries={"total_max_attempts": 1},
            ),
        )
        self.body = (
            body
            if isinstance(body, WireBody)
            else WireBody(payload if body is None else body)
        )
        self.size = len(payload)
        self.status = status
        self.headers = {
            "content-length": str(
                self.size if content_length is None else content_length
            )
        }
        if content_range is not None:
            self.headers["content-range"] = content_range
        self.requests: list[AWSPreparedRequest] = []

    def send(self, request: AWSPreparedRequest, **kwargs: object) -> AWSResponse:
        self.requests.append(request)
        if request.method == "HEAD":
            headers = HTTPHeaders.from_dict({"content-length": str(self.size)})
            return AWSResponse(request.url, 200, headers, WireBody())
        assert request.method == "GET"
        return AWSResponse(
            request.url, self.status, HTTPHeaders.from_dict(self.headers), self.body
        )

    @contextmanager
    def intercept(self) -> Iterator[None]:
        events = self.storage.bucket.meta.client.meta.events
        handler = MagicMock(side_effect=self.send)
        events.register("before-send.s3", handler)
        try:
            with patch.object(evidence, "default_storage", self.storage):
                yield
        finally:
            events.unregister("before-send.s3", handler)


def _verify(vault: ISSVault, payload: bytes) -> evidence.ISSDecision:
    original = evidence.ISSOriginal(
        uuid4(), "evidence.xml", sha256(payload).hexdigest(), len(payload), "2"
    )
    with vault.intercept():
        try:
            return evidence.verify_iss_original(original)
        except (BotoCoreError, ClientError, OSError, ValueError) as error:
            message = f"Retained object read escaped as {error!r}"
            raise AssertionError(message) from error


def test_iss_vault_reads_one_unranged_verified_response() -> None:
    payload = nfse()
    vault = ISSVault(payload)
    assert _verify(vault, payload) == evidence.ISSDecision("2")
    assert [
        (request.method, request.url, request.headers.get("Range"))
        for request in vault.requests
    ] == [("GET", "https://vault.invalid/private-vault/retained/evidence.xml", None)]
    assert vault.body.closed


@pytest.mark.parametrize(
    "fault",
    ["malformed-range", "status", "partial-200", "short", "long", "length", "digest"],
)
def test_iss_vault_unusable_response_is_not_xml_evidence(fault: str) -> None:
    payload = nfse()
    vault = {
        "malformed-range": lambda: ISSVault(
            payload, status=206, content_range="bytes 0-999"
        ),
        "status": lambda: ISSVault(payload, status=202),
        "partial-200": lambda: ISSVault(
            payload, content_range=f"bytes 0-{len(payload) - 1}/{len(payload)}"
        ),
        "short": lambda: ISSVault(payload, body=payload[:-1]),
        "long": lambda: ISSVault(payload, body=payload + b"\n"),
        "length": lambda: ISSVault(payload, content_length=len(payload) + 1),
        "digest": lambda: ISSVault(payload, body=payload.replace(b"12.34", b"42.34")),
    }[fault]()
    with patch.object(evidence, "corroborated_iss_code") as parse:
        actual = _verify(vault, payload)
    assert actual == evidence.ISSDecision(cause=evidence.EVIDENCE_UNAVAILABLE)
    parse.assert_not_called()


@pytest.mark.parametrize(
    "fault", ["slow-down", "connect-timeout", "decode", "mid-stream"]
)
def test_iss_retained_read_exceptions_return_unavailable_without_parsing(
    fault: str,
) -> None:
    payload = nfse()
    body = BrokenBody(payload, prefix=17 if fault == "mid-stream" else 0)
    if fault == "slow-down":
        # The real SDK maps this HTTP error to ClientError (SlowDown).
        xml = b"<Error><Code>SlowDown</Code><Message>synthetic outage</Message></Error>"
        vault = ISSVault(payload, status=503, body=xml, content_length=len(xml))
    else:
        vault = ISSVault(payload, body=body)
    with patch.object(evidence, "corroborated_iss_code") as parse:
        if fault == "connect-timeout":
            timeout = ConnectTimeoutError(endpoint_url="https://vault.invalid")
            with patch.object(vault, "send", side_effect=timeout) as send:
                actual = _verify(vault, payload)
            assert send.call_count == 1
        else:
            actual = _verify(vault, payload)
            assert [request.method for request in vault.requests] == ["GET"]
    assert actual == evidence.ISSDecision(cause=evidence.EVIDENCE_UNAVAILABLE)
    parse.assert_not_called()
    if fault in ("decode", "mid-stream"):
        assert body.attempted
        assert body.closed
        assert body.consumed == body.prefix
