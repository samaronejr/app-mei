"""The WAL archive's on-disk format is a contract between three files that never meet.

`docker-compose.prod.yml` decides how a segment is WRITTEN. `ops/RESTORE.md` is the only
place that says how one is READ. `ops/backup.sh` decides which ones get DELETED. Nothing
at runtime forces the three to agree, and every way they can disagree is silent until
somebody is already restoring:

* a `restore_command` that cannot read `.gz` ends recovery at the first compressed
  segment, and PostgreSQL calls that "end of archive" and promotes;
* a `pg_archivecleanup` without `-x .gz` deletes nothing, so the prune stops bounding
  the archive and the disk fills anyway — the problem compression exists to solve;
* a `restore_command` that handles ONLY `.gz` cannot read the segments already in the
  archive in plain form, so the migration breaks recovery for the retention window.

These assertions are therefore about the *coupling*, not about any one file. They also
make the format migration reviewable: the archive is mixed for `RETENTION_DAYS` after
the flip, and the restore path has to serve both formats for exactly that long.

The `archive_timeout` and `RETENTION_DAYS` assertions are here for a different reason.
Three levers relieve the same capacity pressure — recovery window, RPO, restore
procedure — and this change deliberately spends only the third. Pinning the other two
proves it, and turns any later attempt to quietly also shorten the recovery window into
a failing test rather than a line in a diff nobody reads.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "docker-compose.prod.yml"
BACKUP_SCRIPT = REPO_ROOT / "ops" / "backup.sh"
RESTORE_DOC = REPO_ROOT / "ops" / "RESTORE.md"

ARCHIVE_COMMAND = re.compile(r"^\s*-\s*archive_command=(?P<cmd>.+)$", re.MULTILINE)
ARCHIVE_TIMEOUT = re.compile(
    r"^\s*-\s*archive_timeout=(?P<value>\d+)\s*$", re.MULTILINE
)
RETENTION = re.compile(
    r"^RETENTION_DAYS=\$\{RETENTION_DAYS:-(?P<value>\d+)\}", re.MULTILINE
)
RESTORE_COMMAND = re.compile(r"restore_command = '(?P<cmd>[^']+)'")

SH_BLOCK = re.compile(r"```sh\n(?P<body>.*?)\n```", re.DOTALL)
SED_DELETION = re.compile(r"sed -i\s+'(?P<pattern>[^']+)'")


def archive_command() -> str:
    """Return the one `archive_command` the production compose file sets."""
    matches: list[str] = ARCHIVE_COMMAND.findall(COMPOSE.read_text(encoding="utf-8"))
    assert len(matches) == 1, (
        f"expected exactly one archive_command, found {len(matches)}"
    )
    return matches[0].strip()


STALE_PLAIN_ONLY = "cp /wal_archive/%f %p"


def restore_commands() -> list[str]:
    """Return the `restore_command`s the runbook prescribes; the quoted counterexample
    is excluded by exact match and pinned separately, so going green cannot mean
    deleting the warning."""
    found: list[str] = RESTORE_COMMAND.findall(RESTORE_DOC.read_text(encoding="utf-8"))
    assert found, "ops/RESTORE.md names no restore_command at all"
    prescribed = [command for command in found if command != STALE_PLAIN_ONLY]
    assert prescribed, "ops/RESTORE.md prescribes no restore_command at all"
    return prescribed


def test_the_stale_plain_only_command_is_kept_as_a_counterexample() -> None:
    # Given the runbook
    found = RESTORE_COMMAND.findall(RESTORE_DOC.read_text(encoding="utf-8"))

    # When it is searched for the inherited command a pre-flip base carries
    # Then it is still quoted verbatim, because an operator who cannot recognise it
    # cannot tell whether the base they just extracted is one of the affected ones
    assert STALE_PLAIN_ONLY in found, (
        "the runbook no longer shows the inherited plain-only restore_command, so "
        "restore_commands()'s exclusion now silently hides a real prescription"
    )


def executable_backup_body() -> str:
    """Return ops/backup.sh with comments stripped, so prose cannot satisfy a check."""
    lines = BACKUP_SCRIPT.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("#"))


def canonical_preparation_block() -> str:
    """Return the executable Path A block; a step explained only in prose never runs."""
    blocks = [
        match.group("body")
        for match in SH_BLOCK.finditer(RESTORE_DOC.read_text(encoding="utf-8"))
    ]
    prepared = [block for block in blocks if "postgres:16 bash -c" in block]
    assert len(prepared) == 1, (
        f"expected exactly one canonical Path A block, found {len(prepared)}"
    )
    return prepared[0]


def test_the_canonical_block_deletes_inherited_restore_commands() -> None:
    # Given the block an operator actually executes
    block = canonical_preparation_block()

    # When it is read for the deletion
    deletions = SED_DELETION.findall(block)

    # Then it strips what pg_basebackup copied in. A base taken before the compression
    # flip carries `restore_command = 'cp /wal_archive/%f %p'` verbatim, which now
    # matches nothing, and recovery reporting "end of archive" is what that looks like.
    assert deletions, (
        "the canonical block never deletes the inherited restore_command; a pre-flip "
        "base restores with a plain-only command that matches no segment"
    )
    assert any("restore_command" in pattern for pattern in deletions)


def test_the_deletion_precedes_the_append() -> None:
    # Given the canonical block
    block = canonical_preparation_block()

    # When the deletion and the append are located
    deletion = SED_DELETION.search(block)
    assert deletion is not None
    append = block.index("cat >> /pgdata/postgresql.auto.conf")

    # Then the deletion runs FIRST. Ordering is the whole point: a later assignment does
    # win over an earlier one, so appending alone works only while the appended line
    # parses. Delete-then-append makes a malformed append fail loudly instead of quietly
    # leaving the stale plain-only line in force.
    assert deletion.start() < append, (
        "the sed runs AFTER the append, so a malformed append silently leaves the "
        "inherited plain-only restore_command as the effective setting"
    )


def test_the_canonical_block_appends_exactly_one_dual_format_command() -> None:
    # Given the canonical block
    block = canonical_preparation_block()

    # When its appended restore_command assignments are counted
    appended = RESTORE_COMMAND.findall(block)

    # Then there is exactly one, and it serves both formats
    assert len(appended) == 1, (
        f"expected exactly one appended restore_command, found {len(appended)}"
    )
    command = appended[0]
    assert "%f.gz" in command, (
        f"the canonical restore_command cannot find a compressed segment: {command}"
    )
    assert "gzip -dc" in command, (
        f"the canonical restore_command cannot decompress: {command}"
    )
    assert STALE_PLAIN_ONLY in command, (
        f"the canonical restore_command drops the plain fallback: {command}"
    )


def test_the_deletion_pattern_tolerates_whitespace_around_the_equals() -> None:
    # Given the deletion pattern the runbook actually ships
    deletion = SED_DELETION.search(canonical_preparation_block())
    assert deletion is not None
    posix = deletion.group("pattern").removeprefix("/").removesuffix("/d")
    pattern = re.compile(posix.replace("[[:space:]]", r"\s"))

    # When it is applied to the spacings postgresql.auto.conf actually produces
    # Then every one of them is matched. The file is machine-written and ALTER SYSTEM
    # promises no fixed spacing, so a pattern anchored on `restore_command=` would skip
    # the very line pg_basebackup copies in and leave it as the effective setting.
    for line in (
        "restore_command = 'cp /wal_archive/%f %p'",
        "restore_command='cp /wal_archive/%f %p'",
        "restore_command\t=\t'cp /wal_archive/%f %p'",
    ):
        assert pattern.match(line), f"deletion pattern misses this spacing: {line!r}"

    # And a commented-out line survives, because the anchor is a line start
    assert not pattern.match("#restore_command = 'cp /wal_archive/%f %p'")


def test_the_archive_command_compresses() -> None:
    # Given the production archive_command
    command = archive_command()

    # When it is inspected for the compressor
    # Then it gzips into a .gz name
    assert "gzip" in command
    assert "/wal_archive/%f.gz" in command


def test_every_restore_command_reads_the_compressed_form() -> None:
    # Given an archive_command that writes .gz
    assert "%f.gz" in archive_command()

    # When each documented restore_command is read
    # Then every one of them decompresses
    for command in restore_commands():
        assert "%f.gz" in command, (
            f"restore_command cannot find a compressed segment: {command}"
        )
        assert "gzip -dc" in command, f"restore_command cannot decompress: {command}"


def test_every_restore_command_still_reads_the_plain_form() -> None:
    # Given an archive that stays MIXED for the whole retention window after the flip
    # When each documented restore_command is read
    # Then every one of them also handles a segment with no .gz suffix
    for command in restore_commands():
        assert "cp /wal_archive/%f %p" in command, (
            f"restore_command drops the plain segments already archived: {command}"
        )


def test_the_prune_understands_compressed_segments() -> None:
    # Given an archive_command that writes .gz
    assert "%f.gz" in archive_command()

    # When the pruning invocation is read out of the executable body
    body = executable_backup_body()

    # Then pg_archivecleanup is told the extension, or it silently deletes nothing
    assert "pg_archivecleanup -x .gz" in body


def test_the_archive_command_is_not_a_pipeline() -> None:
    # Given the production archive_command
    command = archive_command()

    # When it is checked for a pipe
    # Then there is none: sh reports only the LAST stage's status, so a failed reader
    # upstream of gzip exits 0 and PostgreSQL discards a segment it never archived
    assert "|" not in command


def test_the_archive_command_publishes_by_atomic_rename() -> None:
    # Given the production archive_command
    command = archive_command()

    # When the write and the publish are examined
    # Then compression targets a scratch name and only a rename creates the segment, so
    # a gzip killed mid-write can never leave a truncated file under the real name
    assert "> /wal_archive/%f.part" in command
    assert "mv /wal_archive/%f.part /wal_archive/%f.gz" in command
    assert command.index("%f.part") < command.index("mv ")


def test_the_archive_command_refuses_to_overwrite_either_format() -> None:
    # Given the production archive_command
    command = archive_command()

    # When its guards are read
    # Then it refuses when the segment is already present in EITHER format, so a
    # re-archive across the migration cannot leave two answers for one segment
    assert "test ! -f /wal_archive/%f " in f"{command} "
    assert "test ! -f /wal_archive/%f.gz" in command


def test_compression_did_not_also_spend_the_rpo() -> None:
    # Given the compose file
    matches = ARCHIVE_TIMEOUT.findall(COMPOSE.read_text(encoding="utf-8"))

    # When archive_timeout is read
    # Then it is unchanged, so the documented <= 5 minute RPO still holds
    assert matches == ["300"]


def test_compression_did_not_also_spend_the_recovery_window() -> None:
    # Given the backup script
    matches = RETENTION.findall(BACKUP_SCRIPT.read_text(encoding="utf-8"))

    # When the retention default is read
    # Then it is unchanged, so the oldest restorable point did not move closer to now
    assert matches == ["7"]
