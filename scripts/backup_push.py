"""Push the newest daily backup to a private GitHub repository (maintainer choice, 2026-10-05: off-machine copy on
GitHub). Run by scripts/backup.cmd after scripts/backup.py.

    python scripts/backup_push.py               # newest complete backup -> PROP_ALPHA_BACKUP_GIT_REMOTE
    python scripts/backup_push.py --keep 14     # keep the newest 14 backup branches on the remote (default)

PROP_ALPHA_BACKUP_GIT_REMOTE (process environment, then .env) is the HTTPS URL of a repository that holds nothing but
backups, e.g. https://github.com/<owner>/prop_alpha-backups.git. Never the code repository: every dump is a ~25 MB
binary that git cannot delta, so a branch that kept them in history would grow by that much every day.

Each backup is pushed as its own branch, `backup/prop_alpha_<UTC stamp>`, holding one commit with no parent: the backup
folder exactly as scripts/backup.py wrote it (dump, state files, manifest). Branches beyond the newest `--keep` are
deleted, and GitHub discards their objects, so the repository stays at about `--keep` backups. Restore one with

    git clone --single-branch --branch backup/prop_alpha_<stamp> <remote> restore
    pg_restore -d <database> restore/prop_alpha_<stamp>/prop_alpha.dump

Fail closed, before anything is pushed:
- the repository must not be public: an unauthenticated GitHub API request must not see it (HTTP 404). A 200 means
  anyone can read the bet ledger, so the push is refused; any other answer (rate limit, outage) refuses too;
- the backup is re-verified against its manifest (SHA-256 of the dump and every state file);
- no file may exceed GitHub's 100 MB file limit (checked at 95 MB, so the refusal is ours, not a half-push).

Credentials are git's own (Git Credential Manager); the URL must carry none. Git never prompts here
(GIT_TERMINAL_PROMPT=0, GCM_INTERACTIVE=never): a scheduled run with no stored credential fails instead of hanging.
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
import tempfile
from pathlib import Path
from typing import Any, Callable, Final, Mapping, Sequence

import requests

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prop_alpha.config import DEFAULT_ENV_FILE, parse_env_file, read_env_file  # noqa: E402

REMOTE_ENV: Final[str] = "PROP_ALPHA_BACKUP_GIT_REMOTE"
DEFAULT_KEEP: Final[int] = 14
BRANCH_PREFIX: Final[str] = "backup/"
MAX_FILE_BYTES: Final[int] = 95 * 1024 * 1024
REMOTE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9-]+)/(?P<repo>[A-Za-z0-9._-]+?)(?:\.git)?$")
BACKUP_NAME: Final[re.Pattern[str]] = re.compile(r"^prop_alpha_\d{8}T\d{6}Z$")
DEFAULT_BRANCH: Final[str] = "main"
COMMIT_NAME: Final[str] = "prop_alpha backup"
COMMIT_EMAIL: Final[str] = "backup@prop-alpha.invalid"
README: Final[str] = (
    "# prop_alpha backups\n\nPrivate. Each `backup/prop_alpha_<UTC stamp>` branch holds one daily backup made by "
    "`scripts/backup.py` and pushed by `scripts/backup_push.py`: `prop_alpha.dump` (pg_dump -Fc of the working "
    "database), `state/` (the bet ledger and alert state files) and `manifest.json` (SHA-256 of each). Only the "
    "newest 14 are kept.\n\nRestore: `git clone --single-branch --branch backup/prop_alpha_<stamp> <this repository> "
    "restore`, then `pg_restore -d <database> restore/prop_alpha_<stamp>/prop_alpha.dump`.\n")
GitRunner = Callable[[Sequence[str], Path], str]
StatusProbe = Callable[[str], int]


def git_environment() -> dict[str, str]:
    """Allow local bare repositories without prompting for credentials."""
    env = dict(os.environ)
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "never",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.bareRepository",
        "GIT_CONFIG_VALUE_0": "all",
    })
    return env


def remote_setting(environ: Mapping[str, str], env_file: Path = DEFAULT_ENV_FILE) -> str:
    """PROP_ALPHA_BACKUP_GIT_REMOTE from the process environment, then .env. Unset raises: the maintainer chose an
    off-machine copy, so a run that cannot make one must fail and be reported, not pass quietly."""
    text = read_env_file(env_file)
    from_file = parse_env_file(text).get(REMOTE_ENV, "") if text is not None else ""
    remote = (environ.get(REMOTE_ENV) or from_file).strip()
    if not remote:
        raise ValueError(f"{REMOTE_ENV} is not set in the environment or in {env_file.name}; the backup stayed on this "
                         "machine")
    parse_remote(remote)
    return remote


def parse_remote(remote: str) -> tuple[str, str]:
    """(owner, repository) of an https://github.com/<owner>/<repo>[.git] URL; anything else (credentials in the URL,
    another host, SSH) raises."""
    match = REMOTE_PATTERN.match(remote)
    if match is None:
        raise ValueError(f"{REMOTE_ENV} must be https://github.com/<owner>/<repository>[.git] with no credentials")
    return match["owner"], match["repo"]


def github_status(url: str) -> int:
    """HTTP status of an unauthenticated GET (no token is ever sent)."""
    return requests.get(url, timeout=20, headers={"Accept": "application/vnd.github+json"}).status_code


def require_private(remote: str, probe: StatusProbe = github_status) -> None:
    """Refuse unless an anonymous request cannot see the repository (404: private, or not created yet, which the push
    then reports)."""
    owner, repo = parse_remote(remote)
    status = probe(f"https://api.github.com/repos/{owner}/{repo}")
    if status == 200:
        raise ValueError(f"{owner}/{repo} is PUBLIC on GitHub: refusing to push the database and bet ledger to it")
    if status != 404:
        raise ValueError(f"could not confirm that {owner}/{repo} is private (GitHub API answered HTTP {status})")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def newest_backup(backup_dir: Path) -> Path:
    """The newest complete backup folder (named prop_alpha_<stamp>, with a manifest); none raises."""
    if not backup_dir.is_dir():
        raise ValueError(f"no backup folder {backup_dir}")
    complete = sorted(path for path in backup_dir.iterdir()
                      if path.is_dir() and BACKUP_NAME.match(path.name) and (path / "manifest.json").is_file())
    if not complete:
        raise ValueError(f"no complete backup in {backup_dir}")
    return complete[-1]


def verify_backup(folder: Path) -> None:
    """Every file the manifest lists matches its SHA-256, nothing else is in the folder, and no file is too large for
    GitHub."""
    manifest: Any = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    expected = {"prop_alpha.dump": manifest["dump"]["sha256"]}
    if "pilot_dump" in manifest:  # the pilot database, when backup.py dumped it (audit 2026-10-11)
        expected["prop_alpha_pilot.dump"] = manifest["pilot_dump"]["sha256"]
    expected.update({f"state/{name}": entry["sha256"] for name, entry in manifest["state"].items()})
    present = {path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_file()} - {"manifest.json"}
    if present != set(expected):
        raise ValueError(f"{folder.name} does not match its manifest: extra {sorted(present - set(expected))}, "
                         f"missing {sorted(set(expected) - present)}")
    for name, digest in sorted(expected.items()):
        path = folder / name
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(f"{folder.name}/{name} is {path.stat().st_size:,} bytes, over GitHub's file limit")
        if sha256(path) != digest:
            raise ValueError(f"{folder.name}/{name} does not match its manifest SHA-256")


def run_git(arguments: Sequence[str], cwd: Path) -> str:
    """Run git; a non-zero exit raises (CalledProcessError) with its stderr.

    Some Git setups treat bare repositories as unsafe unless explicitly allowed. The backup-push workflow uses a local
    bare repo in tests and may also be exercised against a local bare remote during maintenance, so allow this policy
    when running the Git commands here.
    """
    return subprocess.run(["git", "-c", "safe.bareRepository=all", *arguments], cwd=str(cwd), check=True,
                          capture_output=True, text=True, env=git_environment()).stdout


def remote_heads(remote: str, cwd: Path, git: GitRunner) -> list[str]:
    """Every branch name on the remote (without refs/heads/), sorted."""
    listing = git(["ls-remote", "--heads", remote], cwd)
    return sorted(line.split("\trefs/heads/", 1)[1] for line in listing.splitlines() if "\trefs/heads/" in line)


def commit_and_push(source: Path, remote: str, branch: str, message: str, git: GitRunner) -> None:
    """Make `source` a new repository with one commit of everything in it, and push that commit as `branch`."""
    identity = ["-c", f"user.name={COMMIT_NAME}", "-c", f"user.email={COMMIT_EMAIL}"]
    git(["init", "--quiet"], source)
    git([*identity, "add", "--all"], source)
    git([*identity, "commit", "--quiet", "-m", message], source)
    git(["push", "--quiet", remote, f"HEAD:refs/heads/{branch}"], source)


def push_backup(folder: Path, remote: str, keep: int = DEFAULT_KEEP, git: GitRunner = run_git,
                probe: StatusProbe = github_status) -> list[str]:
    """Push `folder` as branch backup/<folder name> (skipped if the remote has it already), then delete the remote's
    backup branches beyond the newest `keep`, never this one. Returns the branches deleted.

    A repository without `main` first gets one holding only a README: GitHub makes the first branch pushed the
    default branch and refuses to delete it, so a backup branch must never be the first."""
    if keep < 1:
        raise ValueError("keep must be at least 1")
    require_private(remote, probe)
    verify_backup(folder)
    branch = BRANCH_PREFIX + folder.name
    with tempfile.TemporaryDirectory(prefix="prop_alpha_backup_push_") as temporary:
        work = Path(temporary)
        heads = remote_heads(remote, work, git)
        if DEFAULT_BRANCH not in heads:
            readme = work / "readme"
            readme.mkdir()
            (readme / "README.md").write_text(README, encoding="utf-8")
            commit_and_push(readme, remote, DEFAULT_BRANCH, "Describe the backup branches", git)
        backups = [name for name in heads
                   if name.startswith(BRANCH_PREFIX) and BACKUP_NAME.match(name.removeprefix(BRANCH_PREFIX))]
        if branch not in backups:
            content = work / "backup"
            shutil.copytree(folder, content / folder.name)
            commit_and_push(content, remote, branch, f"Backup {folder.name}", git)
            backups = sorted([*backups, branch])
        stale = [name for name in backups[:-keep] if name != branch]
        if stale:
            control = work / "control"  # git push needs a repository to run in, even to delete remote branches
            control.mkdir()
            git(["init", "--quiet"], control)
            git(["push", "--quiet", remote, "--delete", *stale], control)
    return stale


def main(argv: Sequence[str] | None = None) -> int:
    from backup import backup_dir_setting  # scripts/ is on sys.path when this file runs as a script

    parser = argparse.ArgumentParser(description="Push the newest daily backup to a private GitHub repository.")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="backup branches to keep (default 14)")
    args = parser.parse_args(argv)
    remote = remote_setting(os.environ)
    folder = newest_backup(backup_dir_setting(os.environ))
    deleted = push_backup(folder, remote, args.keep)
    print(f"pushed {folder.name} to {BRANCH_PREFIX}{folder.name}"
          + (f"; deleted {', '.join(deleted)}" if deleted else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
