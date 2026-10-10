#!/usr/bin/env bash
# prop_alpha CI bootstrap: isolated virtual environment, exact-pinned dependencies, full test matrix,
# and an optional PostgreSQL schema smoke test.
#
# Usage:   ci/bootstrap.sh [--preflight | --release]
#   --preflight   run interpreter and repository checks only; create nothing, install nothing.
#   --release     the release gate: every stage is mandatory. It fails, instead of skipping, when
#                 PROP_ALPHA_CI_DATABASE_URL is unset (stage 6) or pyright cannot run (stage 5).
#
# Environment (all optional):
#   PROP_ALPHA_PYTHON             interpreter used to build the venv          (default: python3)
#   PROP_ALPHA_VENV_DIR           virtual environment location                (default: <repo>/.venv-ci)
#   PROP_ALPHA_ARTIFACT_DIR       pip freeze and JUnit output                 (default: <repo>/ci-artifacts)
#   PROP_ALPHA_CI_DATABASE_URL    disposable PostgreSQL 15+ database named exactly prop_alpha_ci; enables stage 6.
#                                 Inject from the CI secret store. Never commit it. Stage 6 drops the prop_alpha
#                                 schema, so it also requires the database comment CI_DATABASE_MARKER (set once:
#                                 COMMENT ON DATABASE prop_alpha_ci IS '<marker>') and no fixture or player row
#                                 in the database (CI leaves none behind; a working database always has some).
#
# The test suite needs no vendor credentials or production database: tests use in-memory fakes.

set -Eeuo pipefail
IFS=$'\n\t'

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPO_ROOT
readonly PYTHON_BIN="${PROP_ALPHA_PYTHON:-python3}"
readonly VENV_DIR="${PROP_ALPHA_VENV_DIR:-${REPO_ROOT}/.venv-ci}"
readonly ARTIFACT_DIR="${PROP_ALPHA_ARTIFACT_DIR:-${REPO_ROOT}/ci-artifacts}"
readonly PIP_VERSION="26.2.1"
readonly MIN_PYTHON_MAJOR=3
readonly MIN_PYTHON_MINOR=12
readonly CI_DATABASE_NAME="prop_alpha_ci"
readonly CI_DATABASE_MARKER="prop_alpha disposable CI database"
# pyright is not a Python wheel: it runs from npm at this exact version (needs node/npx and the npm registry).
readonly PYRIGHT_VERSION="1.1.406"
readonly EXPECTED_TESTS=710

log() { printf '%s [ci] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "FATAL: $*" >&2; exit 1; }
on_error() {
  local status=$?
  log "FATAL: '${BASH_COMMAND}' exited with status ${status} at line ${BASH_LINENO[0]}" >&2
  exit "${status}"
}
trap on_error ERR

PREFLIGHT_ONLY=0
RELEASE=0
if [[ $# -gt 1 ]]; then
  die "usage: ci/bootstrap.sh [--preflight | --release]"
elif [[ $# -eq 1 ]]; then
  case "$1" in
    --preflight) PREFLIGHT_ONLY=1 ;;
    --release) RELEASE=1 ;;
    *) die "unknown argument '$1'; usage: ci/bootstrap.sh [--preflight | --release]" ;;
  esac
fi
readonly PREFLIGHT_ONLY RELEASE

cd -- "${REPO_ROOT}"

log "stage 1/6: interpreter and repository checks"
command -v "${PYTHON_BIN}" >/dev/null 2>&1 || die "interpreter '${PYTHON_BIN}' not found; set PROP_ALPHA_PYTHON"
if ! "${PYTHON_BIN}" -c "import sys; sys.exit(0 if sys.version_info >= (${MIN_PYTHON_MAJOR}, ${MIN_PYTHON_MINOR}) else 1)"; then
  die "Python >= ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR} is required by the pinned numpy/scipy; found $("${PYTHON_BIN}" -V 2>&1)"
fi
log "interpreter: $("${PYTHON_BIN}" -V 2>&1)"

for required in \
  requirements.txt schema.sql migrations/003_integrity_constraints.sql migrations/004_merge_hardening.sql \
  migrations/005_player_current_team.sql migrations/006_player_attributes_by_kickoff.sql \
  migrations/007_match_position.sql migrations/008_incomplete_team_matches.sql migrations/009_unknown_position.sql \
  prop_alpha/__init__.py tests/test_merge_postgres.sql \
  tests/test_core.py tests/test_integrity.py tests/test_pipeline_concurrency.py tests/test_typing_contract.py \
  mypy.ini pyrightconfig.json; do
  [[ -f "${required}" ]] || die "repository is missing ${required}"
done

unpinned="$(grep -Ev '^[[:space:]]*(#|$)' requirements.txt \
  | grep -Ev '^[A-Za-z0-9._-]+==[^[:space:];]+([[:space:]]*;.*)?$' || true)"
[[ -z "${unpinned}" ]] || die "requirements.txt contains entries not pinned with '==': ${unpinned}"

if [[ "${PREFLIGHT_ONLY}" -eq 1 ]]; then
  log "preflight passed; nothing created or installed"
  exit 0
fi
if [[ "${RELEASE}" -eq 1 ]]; then
  # Refuse before building anything: a release gate that skipped stage 6 would pass on SQL it never ran.
  [[ -n "${PROP_ALPHA_CI_DATABASE_URL:-}" ]] \
    || die "--release requires PROP_ALPHA_CI_DATABASE_URL (a disposable ${CI_DATABASE_NAME} database); stage 6 may not be skipped"
  command -v npx >/dev/null 2>&1 || die "--release requires node/npx to run pyright ${PYRIGHT_VERSION}"
  log "release gate: every stage is mandatory"
fi

log "stage 2/6: fresh virtual environment at ${VENV_DIR}"
if [[ -e "${VENV_DIR}" ]]; then
  [[ -f "${VENV_DIR}/pyvenv.cfg" ]] || die "${VENV_DIR} exists but is not a virtual environment; refusing to delete it"
  rm -rf -- "${VENV_DIR}"
fi
"${PYTHON_BIN}" -m venv "${VENV_DIR}"
if [[ -x "${VENV_DIR}/bin/python" ]]; then
  VENV_PY="${VENV_DIR}/bin/python"
elif [[ -x "${VENV_DIR}/Scripts/python.exe" ]]; then
  VENV_PY="${VENV_DIR}/Scripts/python.exe"
else
  die "virtual environment at ${VENV_DIR} has no interpreter"
fi
readonly VENV_PY

log "stage 3/6: pinned dependencies (binary wheels only)"
export PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 PYTHONHASHSEED=0
"${VENV_PY}" -m pip install --quiet "pip==${PIP_VERSION}"
"${VENV_PY}" -m pip install --quiet --only-binary=:all: --requirement requirements.txt
"${VENV_PY}" -m pip check
mkdir -p -- "${ARTIFACT_DIR}"
"${VENV_PY}" -m pip freeze --all > "${ARTIFACT_DIR}/pip-freeze.txt"

log "stage 4/6: import and byte-compile checks"
"${VENV_PY}" -c "import numpy, scipy, requests, psycopg2, pytest; print('numpy', numpy.__version__, '| scipy', scipy.__version__, '| requests', requests.__version__, '| psycopg2', psycopg2.__version__, '| pytest', pytest.__version__)"
"${VENV_PY}" -m compileall -q prop_alpha tests

log "stage 5/6: test matrix (maths, integrity, typing contract, concurrency) and static types"
"${VENV_PY}" -m pytest tests -vv -p no:cacheprovider --strict-markers --junitxml="${ARTIFACT_DIR}/junit.xml"
"${VENV_PY}" - "${ARTIFACT_DIR}/junit.xml" "${EXPECTED_TESTS}" <<'PY'
import sys
import xml.etree.ElementTree as ET

root = ET.parse(sys.argv[1]).getroot()
suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
totals = {key: sum(int(suite.get(key, 0)) for suite in suites) for key in ("tests", "failures", "errors", "skipped")}
expected = int(sys.argv[2])
print(f"junit totals: {totals}")
if totals["skipped"] or totals["failures"] or totals["errors"] or totals["tests"] < expected:
    sys.exit(f"test matrix incomplete: {totals}; expected at least {expected} executed tests and none skipped")
PY
# Static types (readiness item 4, 2026-10-05): mypy is pinned in requirements.txt and always runs; pyright is the
# checker the audits use. Both read their repository config (mypy.ini, pyrightconfig.json); any error fails.
"${VENV_PY}" -m mypy --config-file mypy.ini
if command -v npx >/dev/null 2>&1; then
  pyright_python="${VENV_PY}"
  # Git Bash paths (/c/Users/...) mean nothing to a Windows node: pass the native form.
  if command -v cygpath >/dev/null 2>&1; then pyright_python="$(cygpath -w "${VENV_PY}")"; fi
  npx --yes "pyright@${PYRIGHT_VERSION}" --pythonpath "${pyright_python}" --project pyrightconfig.json
else
  log "pyright skipped: node/npx not found (the --release gate refuses to skip it)"
fi

if [[ -n "${PROP_ALPHA_CI_DATABASE_URL:-}" ]]; then
  log "stage 6/6: PostgreSQL schema smoke test"
  # The Windows (EDB) installer does not put psql on PATH: fall back to the newest installed version.
  if ! command -v psql >/dev/null 2>&1; then
    pg_bin="$(ls -d "/c/Program Files/PostgreSQL"/*/bin 2>/dev/null | sort -V | tail -n 1 || true)"
    [[ -n "${pg_bin}" ]] && PATH="${pg_bin}:${PATH}"
  fi
  command -v psql >/dev/null 2>&1 || die "psql is required when PROP_ALPHA_CI_DATABASE_URL is set"
  log "psql: $(command -v psql) ($(psql --version))"
  psql_ci() { psql "${PROP_ALPHA_CI_DATABASE_URL}" -X -q -v ON_ERROR_STOP=1 "$@"; }
  db_name="$(psql_ci -At -c 'SELECT current_database()')"
  # Stage 6 drops the prop_alpha schema, so three independent checks must agree this is the disposable database.
  # (1) The exact name: a suffix match would also accept a shared database that merely ends in _ci.
  [[ "${db_name}" == "${CI_DATABASE_NAME}" ]] \
    || die "refusing to reset database '${db_name}': the CI database must be named exactly ${CI_DATABASE_NAME}"
  # (2) A marker an operator sets once, deliberately, on the disposable database only: a renamed copy of the
  # working database, or its DSN pasted into the wrong variable, does not carry it.
  db_marker="$(psql_ci -At -c "SELECT coalesce(shobj_description(oid, 'pg_database'), '') FROM pg_database WHERE datname = current_database()")"
  [[ "${db_marker}" == "${CI_DATABASE_MARKER}" ]] \
    || die "refusing to reset database '${db_name}': it lacks the CI marker; only if it is disposable, run: COMMENT ON DATABASE ${CI_DATABASE_NAME} IS '${CI_DATABASE_MARKER}'"
  # (3) No real data: every CI run leaves fixtures and player_match_stats empty (the merge test rolls back).
  # A table that is not there yet counts as empty. The existence test and the count are separate statements: one
  # statement naming prop_alpha.fixtures fails to parse on a fresh database even in the CASE branch never taken
  # (the first GitHub runs, 2026-10-08: 'relation "prop_alpha.fixtures" does not exist').
  ci_rows=0
  for ci_table in prop_alpha.fixtures prop_alpha.player_match_stats; do
    if [[ "$(psql_ci -At -c "SELECT to_regclass('${ci_table}') IS NOT NULL")" == "t" ]]; then
      ci_rows=$(( ci_rows + $(psql_ci -At -c "SELECT count(*) FROM (SELECT 1 FROM ${ci_table} LIMIT 1) t") ))
    fi
  done
  [[ "${ci_rows}" == "0" ]] \
    || die "refusing to reset database '${db_name}': prop_alpha.fixtures or player_match_stats holds rows, which a CI database never keeps"
  server_version_num="$(psql_ci -At -c 'SHOW server_version_num')"
  (( server_version_num >= 150000 )) || die "PostgreSQL 15+ required; server_version_num=${server_version_num}"
  psql_ci -c 'DROP SCHEMA IF EXISTS prop_alpha CASCADE'
  psql_ci -f schema.sql
  psql_ci -c 'SELECT * FROM prop_alpha.merge_staging_player_feed()'
  # Behaviour on real rows (transfer, backfill, reject); the script rolls itself back.
  psql_ci -At -f tests/test_merge_postgres.sql >/dev/null
  # 005-009 on the current schema must leave it current (006 and 007 reinstall older bodies; 009 restores this one).
  psql_ci -f migrations/005_player_current_team.sql
  psql_ci -f migrations/006_player_attributes_by_kickoff.sql
  psql_ci -f migrations/007_match_position.sql
  psql_ci -f migrations/008_incomplete_team_matches.sql
  psql_ci -f migrations/009_unknown_position.sql
  psql_ci -c 'SELECT * FROM prop_alpha.merge_staging_player_feed()'
  # Upgrade path: 003 reinstalls the v3.2 zero-argument body (an overload of the v3.3 function); 004 must
  # drop it, so the zero-argument call in pipeline.MERGE_SQL resolves to the batched function again. 005
  # then installs the current body, which must behave exactly like a fresh install.
  psql_ci -f migrations/003_integrity_constraints.sql
  psql_ci -f migrations/004_merge_hardening.sql
  psql_ci -f migrations/005_player_current_team.sql
  psql_ci -f migrations/006_player_attributes_by_kickoff.sql
  psql_ci -f migrations/007_match_position.sql
  psql_ci -f migrations/008_incomplete_team_matches.sql
  psql_ci -f migrations/009_unknown_position.sql
  psql_ci -c 'SELECT * FROM prop_alpha.merge_staging_player_feed()'
  psql_ci -c 'SELECT * FROM prop_alpha.merge_staging_player_feed(1)'
  psql_ci -At -f tests/test_merge_postgres.sql >/dev/null
  log "schema smoke test passed on ${db_name}"
else
  log "stage 6/6 skipped: PROP_ALPHA_CI_DATABASE_URL is unset, so schema.sql was not executed in this run (not a release gate)"
fi

if [[ "${RELEASE}" -eq 1 ]]; then
  log "SUCCESS (release gate): pinned environment; ${EXPECTED_TESTS}+ tests, none skipped; mypy and pyright clean; PostgreSQL fresh install, re-apply and upgrade path passed"
else
  log "SUCCESS: environment built from pinned wheels; ${EXPECTED_TESTS}+ tests passed with none skipped"
fi
