"""Observe certificate exclusion through a real Docker COPY, not mount metadata."""

import io
import subprocess
import tarfile
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
SOURCES = (
    "ops/secrets/serpro_contratante_cert.pem",
    "ops/secrets/serpro_contratante_key.pem",
    "ops/secrets/serpro_contratante_key_pass",
)


def test_default_contratante_sources_never_enter_the_application_build(
    tmp_path: Path,
) -> None:
    # Given the actual application ignore rules and synthetic default sources.
    (tmp_path / ".dockerignore").write_bytes((ROOT / ".dockerignore").read_bytes())
    for source in SOURCES:
        target = tmp_path / source
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"synthetic-build-context-marker:{source}\n")
    (tmp_path / "public-control").write_bytes(b"public-source-is-visible\n")
    (tmp_path / "Dockerfile").write_text("FROM scratch\nCOPY . /proof/\n")
    tag = f"axrm013-fix1-context:{uuid4().hex}"
    container = ""
    subprocess.run(  # noqa: S603 - fixed Docker operation with a private context
        ["/usr/bin/docker", "build", "--progress=plain", "-t", tag, str(tmp_path)],
        check=True,
        capture_output=True,
        timeout=120,
    )
    try:
        container = subprocess.run(  # noqa: S603 - inspect without starting an app
            ["/usr/bin/docker", "create", tag, "/synthetic-never-executed"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        # When Docker exports the filesystem produced by the genuine COPY.
        archive = subprocess.run(  # noqa: S603 - exact test-owned container
            ["/usr/bin/docker", "export", container],
            check=True,
            capture_output=True,
            timeout=30,
        )
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as image:
            control = image.extractfile("proof/public-control")
            assert control is not None
            assert control.read() == b"public-source-is-visible\n"
            names = set(image.getnames())
        # Then ordinary source survives but all three private source files do not.
        leaked = [source for source in SOURCES if f"proof/{source}" in names]
        assert leaked == []
    finally:
        if container:
            subprocess.run(  # noqa: S603 - remove only the exact owned container
                ["/usr/bin/docker", "rm", "-f", container],
                check=True,
                capture_output=True,
                timeout=30,
            )
        subprocess.run(  # noqa: S603 - remove only the exact owned image tag
            ["/usr/bin/docker", "image", "rm", "-f", tag],
            check=True,
            capture_output=True,
            timeout=30,
        )
