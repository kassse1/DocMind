"""PostgreSQL backups for DocMind: create, verify, and rehearse a restore safely.

    python scripts/db_backup.py backup --container docmind-postgres --label docmind-dev
    python scripts/db_backup.py verify <file.dump>
    python scripts/db_backup.py restore-drill <file.dump>

backup   pg_dump (custom format) inside the given PostgreSQL container, copied to
         %USERPROFILE%\\.docmind\\backups\\<label>-<UTC timestamp>-rev<alembic>.dump, never
         over an existing file; then checked with pg_restore --list and a SHA-256 file.
verify   read-only: checksum (if a .sha256 file exists) and pg_restore --list, run in a
         throwaway container with no network.
restore-drill
         restores the archive into a NEW throwaway PostgreSQL container: data directory on
         tmpfs (no Docker volume), no network, unique name, removed afterwards. Prints the
         schema revision and row counts per table only, never row contents.

Nothing here restores into, drops or modifies an existing database. Passwords are not
needed (pg_dump runs inside the container as the database owner) and nothing secret is
printed.
"""

import argparse
import hashlib
import re
import secrets
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

PG_IMAGE = (
    "pgvector/pgvector:pg17@sha256:ac08538c6f8b9904c33c8224c5e5706dbe760aca29db1d096972b4052c22a75d"
)
DEFAULT_DEST = Path.home() / ".docmind" / "backups"
DRILL_PREFIX = "docmind-restore-drill-"
_LABEL = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_CONTAINER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_REVISION = re.compile(r"^[0-9a-f]{12}$")

Runner = Callable[..., subprocess.CompletedProcess]


class BackupError(RuntimeError):
    """A step failed; the message is safe to print (no data, no secrets)."""


def run(args: Sequence[str], *, stdin: bytes | None = None, check: bool = True,
        binary: bool = False) -> subprocess.CompletedProcess:
    result = subprocess.run(list(args), input=stdin, capture_output=True, text=not binary)
    if check and result.returncode != 0:
        # The command line holds no secrets; stderr of pg tools holds no row data.
        err = result.stderr if isinstance(result.stderr, str) else result.stderr.decode(
            "utf-8", "replace")
        raise BackupError(f"{args[0]} {args[1]} failed (exit {result.returncode}): "
                          f"{err.strip()[:300]}")
    return result


def checked(pattern: re.Pattern[str], value: str, what: str) -> str:
    if not pattern.match(value):
        raise BackupError(f"invalid {what}: {value!r}")
    return value


def backup_filename(label: str, when: datetime, revision: str | None) -> str:
    checked(_LABEL, label, "label")
    stamp = when.astimezone(UTC).strftime("%Y%m%d-%H%M%SZ")
    rev = f"-rev{revision}" if revision and _REVISION.match(revision) else ""
    return f"{label}-{stamp}{rev}.dump"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def toc_entries(listing: str) -> int:
    """Number of archive entries in `pg_restore --list` output (comment lines start with ;)."""
    return sum(1 for line in listing.splitlines() if line.strip() and not line.startswith(";"))


def table_data_entries(listing: str) -> list[str]:
    """Table names that have a data section in the archive."""
    names = []
    for line in listing.splitlines():
        match = re.search(r"TABLE DATA (\S+) (\S+) ", line)
        if match:
            names.append(f"{match.group(1)}.{match.group(2)}")
    return names


def drill_container_name() -> str:
    return f"{DRILL_PREFIX}{secrets.token_hex(4)}"


def list_archive(dump: Path, runner: Runner = run) -> str:
    """pg_restore --list in a throwaway container with no network; reads the file via stdin."""
    result = runner(["docker", "run", "--rm", "-i", "--network", "none", "--entrypoint",
                     "pg_restore", PG_IMAGE, "--list"], stdin=dump.read_bytes(), binary=True)
    listing = result.stdout.decode("utf-8", "replace")
    if toc_entries(listing) == 0:
        raise BackupError("pg_restore --list read no entries: not a valid custom-format archive")
    return listing


def current_revision(container: str, db: str, user: str, runner: Runner = run) -> str | None:
    result = runner(["docker", "exec", container, "psql", "-U", user, "-d", db, "-At", "-c",
                     "select version_num from alembic_version"], check=False)
    value = (result.stdout or "").strip()
    return value if result.returncode == 0 and _REVISION.match(value) else None


def backup(container: str, db: str, user: str, label: str, dest: Path,
           runner: Runner = run, now: datetime | None = None) -> Path:
    checked(_CONTAINER, container, "container name")
    checked(_IDENT, db, "database name")
    checked(_IDENT, user, "user name")
    dest.mkdir(parents=True, exist_ok=True)
    revision = current_revision(container, db, user, runner)
    name = backup_filename(label, now or datetime.now(UTC), revision)
    target = dest / name
    if target.exists():
        raise BackupError(f"{target} already exists; refusing to overwrite a backup")
    inside = f"/tmp/{name}"
    runner(["docker", "exec", container, "pg_dump", "-U", user, "-d", db, "-Fc", "-f", inside])
    try:
        runner(["docker", "cp", f"{container}:{inside}", str(target)])
        remote = runner(["docker", "exec", container, "sha256sum", inside]).stdout.split()[0]
    finally:
        runner(["docker", "exec", container, "rm", "-f", inside], check=False)
    local = sha256_of(target)
    if local != remote:
        raise BackupError("checksum of the copied file differs from the dump in the container")
    listing = list_archive(target, runner)
    (dest / f"{name}.sha256").write_text(f"{local}  {name}\n", encoding="ascii")
    print(f"backup:   {target}")
    print(f"size:     {target.stat().st_size} bytes")
    print(f"sha256:   {local}")
    print(f"revision: {revision or 'unknown'}")
    tables = len(table_data_entries(listing))
    print(f"archive:  {toc_entries(listing)} entries, data for {tables} tables")
    return target


def verify(dump: Path, runner: Runner = run) -> None:
    if not dump.is_file() or dump.stat().st_size == 0:
        raise BackupError(f"{dump} is missing or empty")
    sums = dump.with_name(dump.name + ".sha256")
    if sums.exists():
        expected = sums.read_text(encoding="ascii").split()[0]
        if sha256_of(dump) != expected:
            raise BackupError("SHA-256 does not match the .sha256 file")
        print("sha256:   matches")
    else:
        print(f"sha256:   {sha256_of(dump)} (no .sha256 file to compare)")
    listing = list_archive(dump, runner)
    print(f"archive:  {toc_entries(listing)} entries, data for "
          f"{len(table_data_entries(listing))} tables: {', '.join(table_data_entries(listing))}")


def restore_drill(dump: Path, runner: Runner = run, timeout_s: float = 60) -> dict[str, int]:
    """Restore into a new container on tmpfs with no network, report counts, remove it."""
    if not dump.is_file():
        raise BackupError(f"{dump} not found")
    name = drill_container_name()
    password = secrets.token_urlsafe(24)  # local to this container; never printed
    runner(["docker", "run", "-d", "--name", name, "--network", "none",
            "--tmpfs", "/var/lib/postgresql/data:size=2g",
            "-e", f"POSTGRES_PASSWORD={password}", "-e", "POSTGRES_DB=drill", PG_IMAGE])
    try:
        mounts = runner(["docker", "inspect", "--format",
                         "{{range .Mounts}}{{.Type}} {{end}}", name]).stdout.split()
        if "volume" in mounts:
            raise BackupError("the drill container got a Docker volume; aborting")
        # Over TCP: the entrypoint's temporary init server listens on the Unix socket only,
        # so this succeeds once the final server is up.
        deadline = time.monotonic() + timeout_s
        while runner(["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "postgres",
                      "-d", "drill"], check=False).returncode != 0:
            if time.monotonic() > deadline:
                raise BackupError("the drill database did not become ready")
            time.sleep(1)
        runner(["docker", "exec", "-i", name, "pg_restore", "-U", "postgres", "-d", "drill",
                "--no-owner", "--no-privileges", "--exit-on-error"],
               stdin=dump.read_bytes(), binary=True)
        query = ("select table_name from information_schema.tables "
                 "where table_schema='public' and table_type='BASE TABLE' order by 1")
        tables = runner(["docker", "exec", name, "psql", "-U", "postgres", "-d", "drill", "-At",
                         "-c", query]).stdout.split()
        counts = {}
        for table in tables:
            checked(_IDENT, table, "table name")
            out = runner(["docker", "exec", name, "psql", "-U", "postgres", "-d", "drill", "-At",
                          "-c", f'select count(*) from public."{table}"']).stdout.strip()
            counts[table] = int(out)
        revision = runner(["docker", "exec", name, "psql", "-U", "postgres", "-d", "drill",
                           "-At", "-c", "select version_num from alembic_version"],
                          check=False).stdout.strip()
        print(f"drill:    restored into {name} (tmpfs, no network)")
        print(f"revision: {revision or 'unknown'}")
        for table, count in counts.items():
            print(f"  {table}: {count} rows")
        return counts
    finally:
        runner(["docker", "rm", "-f", name], check=False)
        print(f"removed:  {name}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("backup", help="pg_dump a running PostgreSQL container")
    b.add_argument("--container", required=True, help="e.g. docmind-postgres (dev stack)")
    b.add_argument("--db", default="docmind")
    b.add_argument("--user", default="docmind")
    b.add_argument("--label", required=True, help="file name prefix, e.g. docmind-dev")
    b.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    v = sub.add_parser("verify", help="check an archive (read-only)")
    v.add_argument("dump", type=Path)
    d = sub.add_parser("restore-drill", help="restore into a throwaway container")
    d.add_argument("dump", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            backup(args.container, args.db, args.user, args.label, args.dest)
        elif args.command == "verify":
            verify(args.dump)
        else:
            restore_drill(args.dump)
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
