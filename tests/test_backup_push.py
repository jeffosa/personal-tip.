"""scripts/backup_push.py: the daily backup's off-machine copy on a private GitHub repository (maintainer choice,
2026-10-05). The push runs real git against a local bare repository standing in for GitHub; only the remote URL is
swapped, so init, commit, push, listing and pruning are the real commands."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Sequence

import pytest

ROOT = Path(__file__).resolve().parents[1]
REMOTE = "https://github.com/jeffosa/prop_alpha-backups.git"
NOW = datetime(2026, 10, 6, 3, 30, tzinfo=timezone.utc)


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"prop_alpha_{name}_script", ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _make_backup(backup_dir: Path, state: Path, at: datetime) -> Path:
    backup = _load("backup")

    def runner(command: Sequence[str]) -> str:
        if "-c" in command:  # row counts, before and after the dump
            return "".join(f"{table}|1\n" for table in backup.REQUIRED_TABLES)
        if "--list" in command:
            return "\n".join(f"1; 0 1 TABLE DATA prop_alpha {table} postgres" for table in backup.REQUIRED_TABLES)
        Path(command[command.index("-f") + 1]).write_bytes(b"PGDMP fake dump " + at.isoformat().encode())
        return ""

    return Path(backup.backup(backup_dir, state, at, runner=runner))


@pytest.fixture()
def state(tmp_path: Path) -> Path:
    folder = tmp_path / "state"
    folder.mkdir()
    (folder / "placed_bets.jsonl").write_text('{"event": "placed", "bet_id": 1}\n', encoding="utf-8")
    return folder


class _Git:
    """Real git, with the GitHub URL replaced by a local bare repository."""

    def __init__(self, bare: Path) -> None:
        self.bare = bare
        self.calls: list[list[str]] = []
        self.env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.bareRepository",
            "GIT_CONFIG_VALUE_0": "all",
        }

    def __call__(self, arguments: Sequence[str], cwd: Path) -> str:
        self.calls.append(list(arguments))
        local = [str(self.bare) if argument == REMOTE else argument for argument in arguments]
        return subprocess.run(["git", *local], cwd=str(cwd), check=True, capture_output=True, text=True,
                              env=self.env).stdout


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.bareRepository",
        "GIT_CONFIG_VALUE_0": "all",
    })
    return env


def _bare(tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(bare)], check=True, env=_git_env())
    return bare


def _heads(bare: Path) -> list[str]:
    out = subprocess.run(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"], cwd=str(bare),
                         check=True, capture_output=True, text=True, env=_git_env()).stdout
    return sorted(out.split())


def _private(url: str) -> int:
    assert url == "https://api.github.com/repos/jeffosa/prop_alpha-backups"
    return 404


def test_the_newest_backup_is_pushed_as_its_own_branch_after_a_readme_main(tmp_path: Path, state: Path) -> None:
    push = _load("backup_push")
    bare = _bare(tmp_path)
    older = _make_backup(tmp_path / "backups", state, NOW - timedelta(days=1))
    newest = _make_backup(tmp_path / "backups", state, NOW)
    assert push.newest_backup(tmp_path / "backups") == newest != older
    assert push.push_backup(newest, REMOTE, git=_Git(bare), probe=_private) == []
    assert _heads(bare) == ["backup/prop_alpha_20261006T033000Z", "main"]
    # main exists first, so GitHub makes it the default branch and a backup branch can always be deleted.
    readme = subprocess.run(["git", "show", "main:README.md"], cwd=str(bare), check=True, capture_output=True,
                            text=True, env=_git_env()).stdout
    assert "Private" in readme and "pg_restore" in readme
    files = subprocess.run(["git", "ls-tree", "-r", "--name-only", "backup/prop_alpha_20261006T033000Z"],
                           cwd=str(bare), check=True, capture_output=True, text=True, env=_git_env()).stdout.split()
    assert sorted(files) == ["prop_alpha_20261006T033000Z/manifest.json", "prop_alpha_20261006T033000Z/prop_alpha.dump",
                             "prop_alpha_20261006T033000Z/state/placed_bets.jsonl"]
    ledger = subprocess.run(["git", "show", "backup/prop_alpha_20261006T033000Z:prop_alpha_20261006T033000Z/state/"
                             "placed_bets.jsonl"], cwd=str(bare), check=True, capture_output=True, text=True,
                            env=_git_env()).stdout
    assert ledger == '{"event": "placed", "bet_id": 1}\n'


def test_a_retried_push_changes_nothing_and_old_branches_beyond_keep_are_deleted(tmp_path: Path, state: Path) -> None:
    push = _load("backup_push")
    bare = _bare(tmp_path)
    folders = [_make_backup(tmp_path / "backups", state, NOW + timedelta(days=day)) for day in range(4)]
    for folder in folders[:3]:
        assert push.push_backup(folder, REMOTE, keep=3, git=_Git(bare), probe=_private) == []
    git = _Git(bare)
    assert push.push_backup(folders[2], REMOTE, keep=3, git=git, probe=_private) == []  # already there: no push
    assert all(call[0] == "ls-remote" for call in git.calls)
    assert push.push_backup(folders[3], REMOTE, keep=3, git=_Git(bare), probe=_private) == [
        "backup/prop_alpha_20261006T033000Z"]
    assert _heads(bare) == ["backup/" + folder.name for folder in folders[1:]] + ["main"]
    with pytest.raises(ValueError, match="at least 1"):
        push.push_backup(folders[3], REMOTE, keep=0, git=_Git(bare), probe=_private)


@pytest.mark.parametrize(("status", "message"), [(200, "PUBLIC"), (403, "HTTP 403"), (500, "HTTP 500")])
def test_nothing_is_pushed_unless_github_hides_the_repository_from_anonymous_requests(
        tmp_path: Path, state: Path, status: int, message: str) -> None:
    push = _load("backup_push")
    folder = _make_backup(tmp_path / "backups", state, NOW)
    git = _Git(_bare(tmp_path))
    with pytest.raises(ValueError, match=message):
        push.push_backup(folder, REMOTE, git=git, probe=lambda url: status)
    assert git.calls == []


def test_a_backup_that_no_longer_matches_its_manifest_is_not_pushed(tmp_path: Path, state: Path) -> None:
    push = _load("backup_push")
    git = _Git(_bare(tmp_path))
    folder = _make_backup(tmp_path / "backups", state, NOW)
    (folder / "state" / "placed_bets.jsonl").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="does not match its manifest SHA-256"):
        push.push_backup(folder, REMOTE, git=git, probe=_private)
    (folder / "state" / "placed_bets.jsonl").unlink()
    with pytest.raises(ValueError, match="missing"):
        push.verify_backup(folder)
    (folder / "state" / "stray.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="extra"):
        push.verify_backup(folder)
    assert git.calls == []


def test_a_file_over_githubs_limit_is_refused_before_pushing(tmp_path: Path, state: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    push = _load("backup_push")
    folder = _make_backup(tmp_path / "backups", state, NOW)
    monkeypatch.setattr(push, "MAX_FILE_BYTES", 10)
    with pytest.raises(ValueError, match="over GitHub's file limit"):
        push.verify_backup(folder)


def test_the_remote_setting_comes_from_the_environment_then_env_and_must_be_a_plain_github_url(
        tmp_path: Path) -> None:
    push = _load("backup_push")
    env_file = tmp_path / ".env"
    with pytest.raises(ValueError, match="not set"):  # chosen off-machine copy: unset fails, it is not skipped
        push.remote_setting({}, env_file)
    env_file.write_text(f"PROP_ALPHA_BACKUP_GIT_REMOTE={REMOTE}\n", encoding="utf-8")
    assert push.remote_setting({}, env_file) == REMOTE
    other = "https://github.com/jeffosa/other"
    assert push.remote_setting({"PROP_ALPHA_BACKUP_GIT_REMOTE": other}, env_file) == other
    assert push.parse_remote(REMOTE) == ("jeffosa", "prop_alpha-backups")
    assert push.parse_remote("https://github.com/jeffosa/prop_alpha-backups") == ("jeffosa", "prop_alpha-backups")
    for bad in ("https://user:token@github.com/jeffosa/x.git", "git@github.com:jeffosa/x.git",
                "https://gitlab.com/jeffosa/x.git", "http://github.com/jeffosa/x.git", "https://github.com/jeffosa"):
        with pytest.raises(ValueError, match="must be https://github.com"):
            push.parse_remote(bad)


def test_git_never_prompts_for_credentials_in_a_scheduled_run(monkeypatch: pytest.MonkeyPatch) -> None:
    push = _load("backup_push")
    seen: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> Any:
        seen.update(kwargs, command=command)
        return type("Completed", (), {"stdout": ""})()

    monkeypatch.setattr(push.subprocess, "run", fake_run)
    push.run_git(["ls-remote", "--heads", REMOTE], ROOT)
    assert seen["command"] == ["git", "-c", "safe.bareRepository=all", "ls-remote", "--heads", REMOTE]
    assert seen["check"] is True
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0" and seen["env"]["GCM_INTERACTIVE"] == "never"


def test_the_manifest_written_by_backup_is_what_the_push_verifies(tmp_path: Path, state: Path) -> None:
    push = _load("backup_push")
    folder = _make_backup(tmp_path / "backups", state, NOW)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["state"]) == {"placed_bets.jsonl"}
    push.verify_backup(folder)  # a fresh backup verifies
    with pytest.raises(ValueError, match="no complete backup"):
        push.newest_backup(tmp_path / "state")
    with pytest.raises(ValueError, match="no backup folder"):
        push.newest_backup(tmp_path / "missing")


def test_a_pilot_dump_is_verified_and_pushed_like_the_main_dump(tmp_path: Path, state: Path) -> None:
    """Audit 2026-10-11, finding 5: the pilot database's dump goes off-machine too, checked against the manifest."""
    backup, push = _load("backup"), _load("backup_push")

    def runner(command: Sequence[str]) -> str:
        if "-c" in command:  # does the pilot database exist? else row counts
            return "1\n" if "pg_database" in command[-1] else "".join(f"{t}|1\n" for t in backup.REQUIRED_TABLES)
        if "--list" in command:
            return "\n".join(f"1; 0 1 TABLE DATA prop_alpha {table} postgres" for table in backup.REQUIRED_TABLES)
        Path(command[command.index("-f") + 1]).write_bytes(b"PGDMP " + command[command.index("-d") + 1].encode())
        return ""

    folder = Path(backup.backup(tmp_path / "backups", state, NOW, runner=runner, pilot_db=backup.PILOT_DB))
    push.verify_backup(folder)
    git = _Git(_bare(tmp_path))
    push.push_backup(folder, REMOTE, git=git, probe=_private)
    pushed = subprocess.run(["git", "-c", "safe.bareRepository=all", "--git-dir", str(git.bare), "ls-tree", "-r",
                             "--name-only", f"backup/{folder.name}"], capture_output=True, text=True, check=True).stdout
    assert f"{folder.name}/{backup.PILOT_DUMP_NAME}" in pushed.split()
    (folder / backup.PILOT_DUMP_NAME).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="does not match its manifest SHA-256"):
        push.verify_backup(folder)
