"""scripts/db_backup.py without Docker: names, safety refusals and the command sequence."""

import importlib.util
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "db_backup", Path(__file__).resolve().parents[1] / "scripts" / "db_backup.py"
)
db_backup = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(db_backup)

LISTING = (
    ";\n; Archive created at 2026-10-27\n;\n"
    "215; 1259 16390 TABLE public users docmind\n"
    "3420; 0 16390 TABLE DATA public users docmind\n"
    "3421; 0 16400 TABLE DATA public documents docmind\n"
)


class FakeDocker:
    """Records docker commands and answers them like a healthy container would."""

    def __init__(self, dump_bytes: bytes = b"PGDMP fixture", listing: str = LISTING):
        self.calls: list[list[str]] = []
        self.dump_bytes = dump_bytes
        self.listing = listing

    def __call__(self, args, *, stdin=None, check=True, binary=False):
        self.calls.append(list(args))
        out: str | bytes = ""
        if args[:2] == ["docker", "cp"]:
            Path(args[3]).write_bytes(self.dump_bytes)
        elif "sha256sum" in args:
            import hashlib

            out = f"{hashlib.sha256(self.dump_bytes).hexdigest()}  /tmp/x\n"
        elif "version_num" in " ".join(args):
            out = "c5d8e0f3a1b6\n"
        elif "--list" in args:
            out = self.listing
        elif "{{range .Mounts}}{{.Type}} {{end}}" in args:
            out = "tmpfs "
        elif "information_schema.tables" in " ".join(args):
            out = "documents\nusers\n"
        elif "count(*)" in " ".join(args):
            out = "2\n"
        if binary and isinstance(out, str):
            out = out.encode()
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")


def test_backup_file_names_are_timestamped_in_utc_and_carry_the_revision():
    when = datetime(2026, 10, 27, 18, 51, 35, tzinfo=UTC)
    assert db_backup.backup_filename("docmind-dev", when, "c5d8e0f3a1b6") == (
        "docmind-dev-20261027-185135Z-revc5d8e0f3a1b6.dump"
    )
    no_rev = db_backup.backup_filename("docmind-dev", when, None)
    assert no_rev == "docmind-dev-20261027-185135Z.dump"
    # A revision that is not an Alembic id is left out rather than put into a path.
    assert "rev" not in db_backup.backup_filename("x", when, "../../etc")


@pytest.mark.parametrize("label", ["", "../up", "Upper", "a b", "x" * 60])
def test_labels_that_could_escape_or_confuse_paths_are_refused(label):
    with pytest.raises(db_backup.BackupError):
        db_backup.backup_filename(label, datetime.now(UTC), None)


def test_toc_parsing_counts_entries_and_data_sections():
    assert db_backup.toc_entries(LISTING) == 3
    assert db_backup.table_data_entries(LISTING) == ["public.users", "public.documents"]
    assert db_backup.toc_entries("; only comments\n") == 0


def test_backup_dumps_copies_verifies_and_never_overwrites(tmp_path, capsys):
    docker = FakeDocker()
    when = datetime(2026, 10, 27, 12, 0, 0, tzinfo=UTC)
    target = db_backup.backup("docmind-postgres", "docmind", "docmind", "docmind-dev", tmp_path,
                              runner=docker, now=when)
    assert target.read_bytes() == b"PGDMP fixture"
    recorded = (tmp_path / f"{target.name}.sha256").read_text().split()[0]
    assert recorded == db_backup.sha256_of(target)
    dump_cmd = next(c for c in docker.calls if "pg_dump" in c)
    assert "-Fc" in dump_cmd and "docmind-postgres" in dump_cmd
    # The temporary file inside the container is removed.
    assert any(c[-3:-1] == ["rm", "-f"] for c in docker.calls)
    # The archive check runs without network access.
    listing_cmd = next(c for c in docker.calls if "--list" in c)
    assert listing_cmd[listing_cmd.index("--network") + 1] == "none"
    assert "revision: c5d8e0f3a1b6" in capsys.readouterr().out

    with pytest.raises(db_backup.BackupError, match="refusing to overwrite"):
        db_backup.backup("docmind-postgres", "docmind", "docmind", "docmind-dev", tmp_path,
                         runner=FakeDocker(), now=when)


def test_backup_fails_when_the_copy_does_not_match_the_dump(tmp_path):
    class Corrupting(FakeDocker):
        def __call__(self, args, **kwargs):
            result = super().__call__(args, **kwargs)
            if args[:2] == ["docker", "cp"]:
                Path(args[3]).write_bytes(b"truncated")
            return result

    with pytest.raises(db_backup.BackupError, match="checksum"):
        db_backup.backup("pg", "docmind", "docmind", "dev", tmp_path, runner=Corrupting())


def test_backup_rejects_unsafe_identifiers(tmp_path):
    with pytest.raises(db_backup.BackupError):
        db_backup.backup("pg; rm -rf /", "docmind", "docmind", "dev", tmp_path, runner=FakeDocker())
    with pytest.raises(db_backup.BackupError):
        db_backup.backup("pg", "docmind; drop", "docmind", "dev", tmp_path, runner=FakeDocker())


def test_verify_detects_a_changed_archive(tmp_path):
    dump = tmp_path / "x.dump"
    dump.write_bytes(b"PGDMP fixture")
    (tmp_path / "x.dump.sha256").write_text("0" * 64 + "  x.dump\n", encoding="ascii")
    with pytest.raises(db_backup.BackupError, match="SHA-256"):
        db_backup.verify(dump, runner=FakeDocker())


def test_verify_rejects_a_file_that_is_not_an_archive(tmp_path):
    dump = tmp_path / "x.dump"
    dump.write_bytes(b"not an archive")
    with pytest.raises(db_backup.BackupError, match="no entries"):
        db_backup.verify(dump, runner=FakeDocker(listing="; nothing\n"))


def test_restore_drill_uses_a_new_tmpfs_container_without_network_and_removes_it(tmp_path):
    dump = tmp_path / "x.dump"
    dump.write_bytes(b"PGDMP fixture")
    docker = FakeDocker()
    counts = db_backup.restore_drill(dump, runner=docker)
    assert counts == {"documents": 2, "users": 2}
    start = next(c for c in docker.calls if c[:3] == ["docker", "run", "-d"])
    name = start[start.index("--name") + 1]
    assert name.startswith(db_backup.DRILL_PREFIX)
    assert start[start.index("--network") + 1] == "none"
    assert any(arg.startswith("/var/lib/postgresql/data") for arg in start)
    assert "-v" not in start and "--volume" not in start and "--mount" not in start
    restore = next(c for c in docker.calls if "pg_restore" in c)
    assert name in restore and "-d" in restore and restore[restore.index("-d") + 1] == "drill"
    assert docker.calls[-1] == ["docker", "rm", "-f", name]


def test_restore_drill_aborts_if_a_volume_was_attached(tmp_path):
    class WithVolume(FakeDocker):
        def __call__(self, args, **kwargs):
            if "{{range .Mounts}}{{.Type}} {{end}}" in args:
                return subprocess.CompletedProcess(args, 0, stdout="volume ", stderr="")
            return super().__call__(args, **kwargs)

    dump = tmp_path / "x.dump"
    dump.write_bytes(b"PGDMP fixture")
    docker = WithVolume()
    with pytest.raises(db_backup.BackupError, match="volume"):
        db_backup.restore_drill(dump, runner=docker)
    assert not any("pg_restore" in c for c in docker.calls)
    assert docker.calls[-1][:3] == ["docker", "rm", "-f"]
