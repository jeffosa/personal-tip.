"""Credentials from the process environment or the gitignored `.env` file at the repository root."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Final, Mapping

DEFAULT_ENV_FILE: Final[Path] = Path(__file__).resolve().parents[1] / ".env"
DSN_ENV: Final[str] = "PROP_ALPHA_DB_DSN"
# The working database (League One/Two), from Sportmonks: back since 2026-10-21 (maintainer chose the Sportmonks Starter
# plan, whose counts match Opta's); `prop_alpha_tsa`, built from TheStatsAPI for 2026-10-15 to 2026-10-20, stays as an
# archive. prop_alpha_ci is reset by every CI run, so the alerts, ledger and status never read it by default.
DEFAULT_DSN: Final[str] = "postgresql://postgres@localhost:5432/prop_alpha"
# The pilot database (sportmonks.PILOT_LEAGUES_WANTED), also Sportmonks again (`prop_alpha_pilot_tsa` is the archive): its
# own results job and scan read and write it, never the working one above.
PILOT_DSN_ENV: Final[str] = "PROP_ALPHA_PILOT_DSN"
DEFAULT_PILOT_DSN: Final[str] = "postgresql://postgres@localhost:5432/prop_alpha_pilot"


def parse_env_file(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines, including files saved with a UTF-8 BOM."""
    values: dict[str, str] = {}
    for number, raw in enumerate(text.removeprefix("\ufeff").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f".env line {number} is not KEY=VALUE")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def read_env_file(env_file: Path = DEFAULT_ENV_FILE) -> str | None:
    return env_file.read_text(encoding="utf-8") if env_file.is_file() else None


def setting(name: str, default: str, environ: Mapping[str, str] | None = None, env_file: Path = DEFAULT_ENV_FILE) -> str:
    """A non-secret setting: the process environment first, then `.env`, then `default`."""
    source = os.environ if environ is None else environ
    text = read_env_file(env_file)
    return source.get(name) or (parse_env_file(text).get(name, "") if text is not None else "") or default


def credential(name: str, environ: Mapping[str, str] | None = None, env_file: Path = DEFAULT_ENV_FILE) -> str:
    """The process environment first, then `.env`; raises if neither has a non-empty value."""
    source = os.environ if environ is None else environ
    text = read_env_file(env_file)
    value = source.get(name) or (parse_env_file(text).get(name, "") if text is not None else "")
    if not value:
        raise ValueError(f"{name} is not set in the environment or in {env_file.name}")
    return value
