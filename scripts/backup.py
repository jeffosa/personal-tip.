"""Daily backup of the working database and the state files (maintainer task 4, 2026-10-05).

    python scripts/backup.py                    # into PROP_ALPHA_BACKUP_DIR, default .prop_alpha_state/backups/daily
    python scripts/backup.py --keep 14          # keep the newest 14 backups (default)

Each run makes one folder `prop_alpha_<UTC stamp>/` holding:
- `prop_alpha.dump`: `pg_dump -Fc` of the working `prop_alpha` database (read-only for the database), checked with
  `pg_restore --list`, which must list the table data of every table in REQUIRED_TABLES;
- `state/`: a copy of every file in STATE_FILES that exists, above all `placed_bets.jsonl`, the only record of the bets
  placed by hand; each copy is re-read and compared by SHA-256 with its source;
- `manifest.json`: sizes and SHA-256 of everything above, written last, so a folder without a manifest is incomplete.

A run that fails any check exits 1 and leaves its folder named `*.incomplete`; pruning removes only complete backup
folders (never anything else in the directory), oldest first, and never the one just made.

The default folder is on this machine's disk: it protects against a bad migration or a corrupt file, not against
losing the disk. Point PROP_ALPHA_BACKUP_DIR (process environment, then .env) at a synced folder (e.g. OneDrive) for
an off-machine copy. A folder set there must already exist: a typo or an unmounted drive would otherwise create a new
local folder and the backups would silently stop leaving the machine. Run daily by scripts/backup.cmd.
The password comes from pgpass (never from this script, its arguments or the repository).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Final, Mapping, Sequence

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prop_alpha.config import DEFAULT_ENV_FILE, parse_env_file, read_env_file  # noqa: E402

STATE_DIR: Final[Path] = ROOT / ".prop_alpha_state"
BACKUP_DIR_ENV: Final[str] = "PROP_ALPHA_BACKUP_DIR"
DEFAULT_BACKUP_DIR: Final[Path] = STATE_DIR / "backups" / "daily"
DEFAULT_KEEP: Final[int] = 14
SOURCE_DB: Final[str] = "prop_alpha"
# The pilot database (Brazil Serie A and the Scottish leagues; audit 2026-10-11, finding 5): dumped beside the working one when
# it exists, so the pilot's history does not depend on a vendor plan to be rebuilt.
PILOT_DB: Final[str] = "prop_alpha_pilot"
PILOT_DUMP_NAME: Final[str] = "prop_alpha_pilot.dump"
_DATABASE_NAME: Final[re.Pattern[str]] = re.compile(r"^[a-z_][a-z0-9_]*$")
PG_BIN: Final[Path] = Path(r"C:\Program Files\PostgreSQL\18\bin")
# The ledger and alert state: losing any of these loses bets, exposure or the reply-to-bet mapping.
STATE_FILES: Final[tuple[str, ...]] = (
    "placed_bets.jsonl", "sent_alerts.jsonl", "alert_messages.jsonl", "alert_deliveries.jsonl", "ledger_answers.jsonl",
    "telegram_offset.json", "ledger_last_poll.json", "closing_quotes.jsonl", "closing_captured.json",
    "scan_fixtures.json", "fair_boards_sent.json", "task_health.json", "fx_rate.json",
    # The evaluation evidence (audit 2026-10-10: both were documented as backed up, and neither was): the model's
    # logged prospective predictions (Gate 2 lockbox) and the T-60 team sheets.
    "predictions.jsonl", "lineups_t60.jsonl", "lineups_t60_handled.json",
    # The pilot scan's evidence (audit 2026-10-11): its alerts, paper bets, tips, logged predictions, closing prices and the
    # matches the data feed refused. The pilot database itself is not dumped here.
    "pilot_sent_alerts.jsonl", "pilot_alert_messages.jsonl", "pilot_alert_deliveries.jsonl", "pilot_paper_bets.jsonl",
    "pilot_tips.jsonl", "pilot_predictions.jsonl", "pilot_closing_quotes.jsonl", "pilot_closing_handled.json",
    "pilot_refused.json",
)
# Folders copied whole: the raw bookmaker responses are the evidence for closing-line analysis (PRD Gate 3).
STATE_DIRS: Final[tuple[str, ...]] = ("odds_archive",)
REQUIRED_TABLES: Final[tuple[str, ...]] = (
    "fixtures", "player_match_stats", "players", "teams", "leagues", "raw_payloads", "incomplete_team_matches",
)
BACKUP_NAME: Final[re.Pattern[str]] = re.compile(r"^prop_alpha_\d{8}T\d{6}Z$")
Runner = Callable[[Sequence[str]], str]


def backup_dir_setting(environ: Mapping[str, str], env_file: Path = DEFAULT_ENV_FILE) -> Path:
    """PROP_ALPHA_BACKUP_DIR from the process environment, then .env; the default folder when neither sets it. A folder
    that is set must exist."""
    text = read_env_file(env_file)
    from_file = parse_env_file(text).get(BACKUP_DIR_ENV, "") if text is not None else ""
    raw = (environ.get(BACKUP_DIR_ENV) or from_file).strip()
    if not raw:
        return DEFAULT_BACKUP_DIR
    folder = Path(raw)
    if not folder.is_dir():
        raise ValueError(f"{BACKUP_DIR_ENV}={raw!r} is not an existing folder; create it, or fix the setting")
    return folder


def run(command: Sequence[str]) -> str:
    """Run a PostgreSQL client tool; a non-zero exit raises (CalledProcessError) with its stderr."""
    return subprocess.run(list(command), check=True, capture_output=True, text=True).stdout


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dump_commands(target: Path, database: str = SOURCE_DB) -> tuple[list[str], list[str]]:
    """The pg_dump and pg_restore --list commands; -w never prompts, so a missing pgpass entry fails instead."""
    return ([str(PG_BIN / "pg_dump"), "-w", "-h", "localhost", "-U", "postgres", "-d", database, "-Fc", "-f",
             str(target)], [str(PG_BIN / "pg_restore"), "--list", str(target)])


def database_exists(name: str, runner: Runner) -> bool:
    """Whether the server has a database of this name (one read-only query; the name must be a plain identifier)."""
    if _DATABASE_NAME.match(name) is None:
        raise ValueError(f"{name!r} is not a plain database name")
    query = f"SELECT 1 FROM pg_database WHERE datname = '{name}'"
    return runner([str(PG_BIN / "psql"), "-w", "-h", "localhost", "-U", "postgres", "-d", "postgres", "-X", "-At", "-c",
                   query]).strip() == "1"


def check_dump_listing(listing: str) -> None:
    """Every required table's data must be in the dump's table of contents."""
    present = set(re.findall(r"TABLE DATA prop_alpha (\w+) ", listing))
    missing = [table for table in REQUIRED_TABLES if table not in present]
    if missing:
        raise ValueError(f"dump is missing table data for: {', '.join(missing)}")


def copy_state(state_dir: Path, target: Path) -> dict[str, dict[str, object]]:
    """Copy the existing STATE_FILES and every file under STATE_DIRS, verify each copy by SHA-256, and return
    relative name -> {bytes, sha256}."""
    target.mkdir(parents=True, exist_ok=True)
    names = [name for name in STATE_FILES if (state_dir / name).is_file()]
    for folder in STATE_DIRS:
        if (state_dir / folder).is_dir():
            names += sorted(path.relative_to(state_dir).as_posix() for path in (state_dir / folder).rglob("*")
                            if path.is_file())
    copied: dict[str, dict[str, object]] = {}
    for name in names:
        source, copy = state_dir / name, target / name
        copy.parent.mkdir(parents=True, exist_ok=True)
        expected = sha256(source)
        shutil.copy2(source, copy)
        if sha256(copy) != expected:
            raise ValueError(f"copy of {name} does not match its source (was it written during the backup?)")
        copied[name] = {"bytes": source.stat().st_size, "sha256": expected}
    return copied


def prune(backup_dir: Path, keep: int, current: Path) -> list[Path]:
    """Remove complete backup folders beyond the newest `keep`; never `current`, an incomplete folder, or anything
    whose name is not a backup folder name. Returns what was removed."""
    if keep < 1:
        raise ValueError("keep must be at least 1")
    complete = sorted(path for path in backup_dir.iterdir()
                      if path.is_dir() and BACKUP_NAME.match(path.name) and (path / "manifest.json").is_file())
    removable = [path for path in complete[:-keep] if path != current]
    for path in removable:
        shutil.rmtree(path)
    return removable


def backup(backup_dir: Path, state_dir: Path, now: datetime, keep: int = DEFAULT_KEEP,
           runner: Runner = run, pilot_db: str | None = None) -> Path:
    """Make one verified backup and prune old ones; returns the backup folder. With `pilot_db`, that database is dumped and
    checked the same way as `prop_alpha.dump` when it exists (`PILOT_DUMP_NAME`, `pilot_dump` in the manifest); a server
    without it is noted, not an error."""
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final = backup_dir / f"prop_alpha_{stamp}"
    work = backup_dir / f"prop_alpha_{stamp}.incomplete"
    if final.exists() or work.exists():
        raise ValueError(f"{final.name} already exists")
    work.mkdir(parents=True)
    dump = work / "prop_alpha.dump"
    dump_command, list_command = dump_commands(dump)
    runner(dump_command)
    check_dump_listing(runner(list_command))
    pilot_entry: dict[str, object] | None = None
    if pilot_db is not None:
        if database_exists(pilot_db, runner):
            pilot_dump = work / PILOT_DUMP_NAME
            pilot_command, pilot_list = dump_commands(pilot_dump, pilot_db)
            runner(pilot_command)
            check_dump_listing(runner(pilot_list))
            pilot_entry = {"database": pilot_db, "bytes": pilot_dump.stat().st_size, "sha256": sha256(pilot_dump)}
        else:
            print(f"note: no {pilot_db} database on this server; not dumped")
    state = copy_state(state_dir, work / "state")
    if not (state_dir / "placed_bets.jsonl").is_file():
        print("note: placed_bets.jsonl does not exist yet (no bet recorded); nothing to copy for it")
    manifest = {"created_at": now.astimezone(timezone.utc).isoformat(), "database": SOURCE_DB,
                "dump": {"bytes": dump.stat().st_size, "sha256": sha256(dump)}, "state": state,
                **({"pilot_dump": pilot_entry} if pilot_entry is not None else {})}
    (work / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    work.rename(final)
    for removed in prune(backup_dir, keep, final):
        print(f"pruned {removed.name}")
    return final


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Back up the working database and the ledger/alert state files.")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="complete backups to keep (default 14)")
    args = parser.parse_args(argv)
    backup_dir = backup_dir_setting(os.environ)
    folder = backup(backup_dir, STATE_DIR, datetime.now(timezone.utc), args.keep, pilot_db=PILOT_DB)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    pilot = manifest.get("pilot_dump")
    print(f"backup complete: {folder} (dump {manifest['dump']['bytes']:,} bytes; "
          + (f"pilot dump {pilot['bytes']:,} bytes; " if pilot else "no pilot dump; ") + "state files: "
          f"{', '.join(manifest['state']) or 'none'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
