"""Restore one backup made by scripts/backup.py and check it (maintainer decision 4, 2026-10-11: test a restore on the
always-on host before it takes over).

    git clone --single-branch --branch backup/prop_alpha_<stamp> <backup remote> restore
    python scripts/restore_backup.py restore/prop_alpha_<stamp>                         # prop_alpha (+ pilot) and state
    python scripts/restore_backup.py restore/prop_alpha_<stamp> --database prop_alpha_restore_test --no-state
                                                                                         # a throwaway check only

Fail closed, in this order, before anything is written:
- the manifest must exist and every file in it (the dumps and each state file) must match its size and SHA-256;
- the target database (and the pilot one) must not exist yet: this never restores over a database;
- with state, no state file may already exist in the target folder: this never overwrites a ledger.
Then each dump is restored with `pg_restore --exit-on-error --no-owner` into a database created for it, and the row
count of every required table is compared with the counts in the manifest (backups made before 2026-10-11 have none;
their counts are printed, not checked). Any difference exits 1. The password comes from pgpass, never from here.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backup import (  # noqa: E402  (scripts/ is on sys.path when this file runs as a script)
    PILOT_DB, PILOT_DUMP_NAME, REQUIRED_TABLES, SOURCE_DB, STATE_DIR, Runner, database_exists, pg_tool, run, sha256,
    table_counts,
)

MANIFEST: Final[str] = "manifest.json"


def verify(folder: Path) -> dict[str, Any]:
    """The manifest, after checking every file it lists (size and SHA-256)."""
    if not (folder / MANIFEST).is_file():
        raise ValueError(f"{folder} has no {MANIFEST}: not a complete backup")
    manifest: dict[str, Any] = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    entries: list[tuple[Path, Mapping[str, Any]]] = [(folder / "prop_alpha.dump", manifest["dump"])]
    if "pilot_dump" in manifest:
        entries.append((folder / PILOT_DUMP_NAME, manifest["pilot_dump"]))
    entries += [(folder / "state" / name, entry) for name, entry in manifest["state"].items()]
    for path, entry in entries:
        if not path.is_file():
            raise ValueError(f"{path.relative_to(folder)} is listed in the manifest but missing")
        if path.stat().st_size != int(entry["bytes"]) or sha256(path) != entry["sha256"]:
            raise ValueError(f"{path.relative_to(folder)} does not match the manifest")
    return manifest


def restore_database(dump: Path, database: str, expected: Mapping[str, int] | None, runner: Runner) -> dict[str, int]:
    """Create `database` (it must not exist), restore `dump` into it and compare the row counts with `expected`."""
    if database_exists(database, runner):
        raise ValueError(f"database {database} already exists; this never restores over a database")
    runner([pg_tool("createdb"), "-w", "-h", "localhost", "-U", "postgres", database])
    runner([pg_tool("pg_restore"), "-w", "-h", "localhost", "-U", "postgres", "-d", database, "--no-owner",
            "--exit-on-error", str(dump)])
    counts = table_counts(database, runner)
    if expected is not None and dict(expected) != counts:
        differ = ", ".join(f"{t} {counts[t]} (backup {expected.get(t)})" for t in REQUIRED_TABLES
                           if counts[t] != expected.get(t))
        raise ValueError(f"restored {database} does not match the backup: {differ}")
    return counts


def restore_state(folder: Path, names: Sequence[str], state_dir: Path) -> None:
    """Copy the backed-up state files into `state_dir`; refuses if any of them already exists there."""
    present = [name for name in names if (state_dir / name).exists()]
    if present:
        raise ValueError(f"{state_dir} already holds {', '.join(present[:5])}{' ...' if len(present) > 5 else ''}; "
                         "this never overwrites state files")
    for name in names:
        target = state_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(folder / "state" / name, target)
        if sha256(target) != sha256(folder / "state" / name):
            raise ValueError(f"copy of {name} does not match the backup")


def restore(folder: Path, database: str, pilot_database: str, state_dir: Path | None, runner: Runner) -> list[str]:
    """Verify, then restore the databases and (unless `state_dir` is None) the state files; returns report lines."""
    manifest = verify(folder)
    targets = [database] + ([pilot_database] if "pilot_dump" in manifest else [])
    if len(set(targets)) != len(targets):
        raise ValueError("the working and pilot databases need different names")
    for name in targets:  # every refusal before the first write
        if database_exists(name, runner):
            raise ValueError(f"database {name} already exists; this never restores over a database")
    names = sorted(manifest["state"])
    if state_dir is not None:
        present = [name for name in names if (state_dir / name).exists()]
        if present:
            raise ValueError(f"{state_dir} already holds {', '.join(present[:5])}; this never overwrites state files")
    lines: list[str] = []
    for dump, name, entry in [(folder / "prop_alpha.dump", database, manifest["dump"]),
                              *([(folder / PILOT_DUMP_NAME, pilot_database, manifest["pilot_dump"])]
                                if "pilot_dump" in manifest else [])]:
        expected = entry.get("counts")
        counts = restore_database(dump, name, expected, runner)
        shown = ", ".join(f"{table} {counts[table]:,}" for table in REQUIRED_TABLES)
        lines.append(f"{name}: {shown} ({'match the backup' if expected is not None else 'no counts in this backup to check'})")
    if state_dir is not None:
        restore_state(folder, names, state_dir)
        lines.append(f"state: {len(names)} files restored into {state_dir}")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restore and check one backup made by scripts/backup.py.")
    parser.add_argument("folder", type=Path, help="the backup folder (prop_alpha_<stamp>, holding manifest.json)")
    parser.add_argument("--database", default=SOURCE_DB, help=f"database to create (default {SOURCE_DB})")
    parser.add_argument("--pilot-database", default=PILOT_DB, help=f"pilot database to create (default {PILOT_DB})")
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR, help="where the state files go")
    parser.add_argument("--no-state", action="store_true", help="restore the databases only (a throwaway check)")
    args = parser.parse_args(argv)
    for line in restore(args.folder, args.database, args.pilot_database, None if args.no_state else args.state_dir, run):
        print(line)
    print("restore complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
