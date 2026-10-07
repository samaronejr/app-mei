"""Parse real archives; metadata estimates never authorize oversized input."""

import hashlib
import io
import json
import os
import zipfile
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path
from secrets import token_hex
from types import SimpleNamespace
from typing import IO, TYPE_CHECKING
from uuid import UUID

import pytest
from django.core.files import File
from django.core.files.storage import FileSystemStorage
from storages.backends.s3 import S3Storage

from apps.cycles.evidence_zip import Original, ZipError, build_archive
from config.settings import prod

if TYPE_CHECKING:
    from apps.cycles.effects.manifest import JSONValue

DOCUMENT_ID = UUID("00000000-0000-4000-8000-000000000018")
SECOND_ID = UUID("00000000-0000-4000-8000-000000000019")
CONTENT = b"%PDF-1.7\nsynthetic original bytes\n%%EOF"


def original(root: Path, *, name: str = "nota.pdf") -> Original:
    (root / "source.pdf").write_bytes(CONTENT)
    return Original(
        id=DOCUMENT_ID,
        key="source.pdf",
        sha256=hashlib.sha256(CONTENT).hexdigest(),
        size=len(CONTENT),
        content_type="application/pdf",
        filename=name,
    )


def test_closed_archive_digest_manifest_and_original_are_exact(tmp_path: Path) -> None:
    source = original(tmp_path)
    index: dict[str, JSONValue] = {
        "schema": "axorim.evidence.v1",
        "documents": [{"id": str(DOCUMENT_ID)}],
    }
    with build_archive(index, (source,), storage=FileSystemStorage(tmp_path)) as bundle:
        raw = bundle.file.read()
        assert len(raw) == bundle.size
        assert hashlib.sha256(raw).hexdigest() == bundle.sha256
        bundle.file.seek(0)
        with zipfile.ZipFile(bundle.file) as archive:
            assert set(archive.namelist()) == {
                "index.json",
                "index.html",
                f"originals/{DOCUMENT_ID}.pdf",
                "manifest.sha256",
            }
            assert archive.read(f"originals/{DOCUMENT_ID}.pdf") == CONTENT
            assert json.loads(archive.read("index.json")) == index
            manifest = archive.read("manifest.sha256").decode().splitlines()
            assert len(manifest) == 3
            for line in manifest:
                digest, member = line.split("  ", 1)
                assert digest == hashlib.sha256(archive.read(member)).hexdigest()
                assert member != "manifest.sha256"
            assert all(
                member.compress_type == zipfile.ZIP_STORED
                for member in archive.infolist()
            )
        handle = bundle.file
    assert handle.closed
    assert (tmp_path / "source.pdf").read_bytes() == CONTENT


@pytest.mark.parametrize(
    "name",
    [
        "../../outside.pdf",
        "/absolute.pdf",
        "C:\\outside.pdf",
        "nota.pdf\ninjected  other",
        "nota\u202e.pdf",
        '"><script>alert(1)</script>.pdf',
    ],
)
def test_untrusted_filenames_never_determine_members(tmp_path: Path, name: str) -> None:
    source = original(tmp_path, name=name)
    second = replace(source, id=SECOND_ID)
    with (
        build_archive(
            {"documents": []},
            (source, second),
            storage=FileSystemStorage(tmp_path),
        ) as bundle,
        zipfile.ZipFile(bundle.file) as archive,
    ):
        originals = [
            name for name in archive.namelist() if name.startswith("originals/")
        ]
        assert originals == [
            f"originals/{DOCUMENT_ID}.pdf",
            f"originals/{SECOND_ID}.pdf",
        ]


def test_relinked_document_has_one_original(tmp_path: Path) -> None:
    source = original(tmp_path)
    links: list[JSONValue] = [{"document_id": str(DOCUMENT_ID)}] * 2
    with (
        build_archive(
            {"links": links},
            (source, source),
            storage=FileSystemStorage(tmp_path),
        ) as bundle,
        zipfile.ZipFile(bundle.file) as archive,
    ):
        assert archive.namelist().count(f"originals/{DOCUMENT_ID}.pdf") == 1
        assert len(json.loads(archive.read("index.json"))["links"]) == 2


def test_actual_input_cap_ignores_lying_metadata(tmp_path: Path) -> None:
    source = original(tmp_path)
    large = b"a" * 20000
    (tmp_path / source.key).write_bytes(large)
    source = replace(source, size=1, sha256=hashlib.sha256(large).hexdigest())
    with (
        pytest.raises(ZipError, match="byte_cap"),
        build_archive(
            {"documents": []},
            (source,),
            storage=FileSystemStorage(tmp_path),
            byte_cap=4096,
        ),
    ):
        pytest.fail("An oversized source became a bundle.")
    assert (tmp_path / source.key).stat().st_size == 20000


def test_cap_includes_index_and_closed_central_directory(tmp_path: Path) -> None:
    with build_archive({}, (), storage=FileSystemStorage(tmp_path)) as control:
        exact_size = control.size
    with build_archive(
        {}, (), storage=FileSystemStorage(tmp_path), byte_cap=exact_size
    ) as boundary:
        assert boundary.size == exact_size
    with (
        pytest.raises(ZipError, match="byte_cap"),
        build_archive(
            {}, (), storage=FileSystemStorage(tmp_path), byte_cap=exact_size - 1
        ),
    ):
        pytest.fail("The central directory exceeded the cap.")


@pytest.mark.parametrize("mode", ["missing", "short", "changed", "empty"])
def test_incomplete_original_never_produces_ready_bundle(
    tmp_path: Path, mode: str
) -> None:
    source = original(tmp_path)
    if mode == "missing":
        (tmp_path / source.key).unlink()
    else:
        replacement = {
            "short": CONTENT[:-1],
            "changed": b"x" * len(CONTENT),
            "empty": b"",
        }[mode]
        (tmp_path / source.key).write_bytes(replacement)
    with (
        pytest.raises(ZipError),
        build_archive({}, (source,), storage=FileSystemStorage(tmp_path)),
    ):
        pytest.fail("An unavailable or altered original was exported.")


class ParsedHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def test_static_html_keeps_hostile_text_inert(tmp_path: Path) -> None:
    hostile = '</pre><script>alert(1)</script><img src="https://outside.test/">'
    source = original(tmp_path, name=hostile)
    with (
        build_archive(
            {"display": hostile},
            (source,),
            storage=FileSystemStorage(tmp_path),
        ) as bundle,
        zipfile.ZipFile(bundle.file) as archive,
    ):
        parsed = ParsedHTML()
        parsed.feed(archive.read("index.html").decode())
    assert hostile in "".join(parsed.text)
    for tag, attributes in parsed.tags:
        assert tag not in {"script", "iframe", "object", "embed", "form", "img"}
        assert not any(name.startswith("on") for name in attributes)
        for name in ("href", "src"):
            if attributes.get(name):
                assert str(attributes[name]).startswith("originals/")


def test_spool_rolls_to_disk_without_materializing_original(tmp_path: Path) -> None:
    path = tmp_path / "source.pdf"
    digest = hashlib.sha256()
    chunk = b"q" * 65536
    with path.open("wb") as handle:
        for _ in range(257):
            handle.write(chunk)
            digest.update(chunk)
    source = Original(
        id=DOCUMENT_ID,
        key=path.name,
        sha256=digest.hexdigest(),
        size=257 * len(chunk),
        content_type="application/pdf",
        filename="large.pdf",
    )
    with build_archive({}, (source,), storage=FileSystemStorage(tmp_path)) as bundle:
        assert isinstance(bundle.file.name, int)
        assert os.fstat(bundle.file.name).st_size == bundle.size
        assert bundle.size > 16 * 1024 * 1024


def test_production_s3_adapter_spills_before_caller_consumes_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[IO[bytes]] = []
    spilled: list[bool] = []
    chunk = b"s" * 65536

    class RemoteObject:
        content_length = 257 * len(chunk)

        def load(self, **parameters: object) -> None:
            return None

        def download_fileobj(
            self, destination: IO[bytes], **parameters: object
        ) -> None:
            captured.append(destination)
            for _ in range(257):
                destination.write(chunk)
            spilled.append(destination.name is not None)
            assert parameters["Config"] is backend.transfer_config

    configuration = prod.STORAGES["default"]["OPTIONS"]
    assert isinstance(configuration, dict)
    options = dict(configuration)
    options.update(
        bucket_name="synthetic-t18",
        access_key="synthetic",
        secret_key=token_hex(16),
        region_name="us-east-1",
        endpoint_url="https://storage.example.test",
    )
    backend = S3Storage(**options)
    monkeypatch.setattr(
        backend, "_bucket", SimpleNamespace(Object=lambda key: RemoteObject())
    )
    with backend.open("original.pdf", "rb") as original_file:
        assert original_file.read(65536) == chunk
        assert spilled == [True]
    assert len(captured) == 1
    assert captured[0].closed


def test_s3_original_counter_stops_network_body_at_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Body:
        def __init__(self) -> None:
            self.consumed = 0
            self.closed = False

        def read(self, size: int) -> bytes:
            count = min(size, 10000 - self.consumed)
            self.consumed += count
            return b"z" * count

        def close(self) -> None:
            self.closed = True

    body = Body()

    class RemoteObject:
        content_length = 1

        def load(self, **parameters: object) -> None:
            return None

        def get(self) -> dict[str, Body]:
            return {"Body": body}

        def download_fileobj(
            self, destination: IO[bytes], **parameters: object
        ) -> None:
            # Preserve the eager adapter's behavior so a regression fails on
            # bytes consumed, not because the transport double lacks a method.
            while chunk := body.read(65536):
                destination.write(chunk)
            body.close()

    configuration = prod.STORAGES["default"]["OPTIONS"]
    assert isinstance(configuration, dict)
    backend = S3Storage(**configuration)
    monkeypatch.setattr(
        backend, "_bucket", SimpleNamespace(Object=lambda key: RemoteObject())
    )
    source = Original(
        DOCUMENT_ID, "original.pdf", "0" * 64, 1, "application/pdf", "original.pdf"
    )
    with (
        pytest.raises(ZipError, match="byte_cap"),
        build_archive({}, (source,), storage=backend, byte_cap=4096),
    ):
        pytest.fail("An oversized remote body was accepted.")
    assert body.consumed == 4097
    assert body.closed


def test_original_read_failure_closes_real_file_without_deleting_it(
    tmp_path: Path,
) -> None:
    source = original(tmp_path)
    opened: list[io.BufferedReader] = []

    class BrokenRead(io.BufferedReader):
        def read(self, size: int | None = -1, /) -> bytes:
            message = "synthetic original read failure"
            raise OSError(message)

    class ReadFailureStorage(FileSystemStorage):
        def _open(self, name: str, mode: str = "rb") -> File[bytes]:
            stream = BrokenRead(io.FileIO(self.path(name), mode))
            opened.append(stream)
            return File(stream)

    with (
        pytest.raises(ZipError, match="original_unavailable") as failure,
        build_archive({}, (source,), storage=ReadFailureStorage(tmp_path)),
    ):
        pytest.fail("A failed original read yielded a bundle.")
    assert len(opened) == 1
    assert opened[0].closed
    assert isinstance(failure.value.__cause__, OSError)
    assert (tmp_path / source.key).read_bytes() == CONTENT
