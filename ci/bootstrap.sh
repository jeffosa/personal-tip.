#!/usr/bin/env bash
# prop_alpha CI bootstrap: isolated virtual environment, exact-pinned dependencies, full test matrix,
# and an optional PostgreSQL schema smoke test.
#
# Usage:   ci/bootstrap.sh [--preflight]
#   --preflight   run interpreter and repository checks only; create nothing, install nothing.
#
# Environment (all optional):
#   PROP_ALPHA_PYTHON             interpreter used to build the venv          (default: python3)
#   PROP_ALPHA_VENV_DIR           virtual environment location                (default: <repo>/.venv-ci)
#   PROP_ALPHA_ARTIFACT_DIR       pip freeze and JUnit output                 (default: <repo>/ci-artifacts)
#   PROP_ALPHA_CI_DATABASE_URL    disposable PostgreSQL 15+ database named exactly prop_alpha_ci; enables stage 6.
#                                 Inject from the CI secret store. Never commit it.
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
readonly EXPECTED_TESTS=460

log() { printf '%s [ci] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "FATAL: $*" >&2; exit 1; }
on_error() {
  local status=$?
  log "FATAL: '${BASH_COMMAND}' exited with status ${status} at line ${BASH_LINENO[0]}" >&2
  exit "${status}"
}
trap on_error ERR

PREFLIGHT_ONLY=0
if [[ $# -gt 1 ]]; then
  die "usage: ci/bootstrap.sh [--preflight]"
elif [[ $# -eq 1 ]]; then
  [[ "$1" == "--preflight" ]] || die "unknown argument '$1'; usage: ci/bootstrap.sh [--preflight]"
  PREFLIGHT_ONLY=1
fi
readonly PREFLIGHT_ONLY

cd -- "${REPO_ROOT}"

log "stage 1/6: interpreter and repository checks"
command -v "${PYTHON_BIN}" >/dev/null 2>&1 || die "interpreter '${PYTHON_BIN}' not found; set PROP_ALPHA_PYTHON"
if ! "${PYTHON_BIN}" -c "import sys; sys.exit(0 if sys.version_info >= (${MIN_PYTHON_MAJOR}, ${MIN_PYTHON_MINOR}) else 1)"; then
  die "Python >= ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR} is required by the pinned numpy/scipy; found $("${PYTHON_BIN}" -V 2>&1)"
fi
log "interpreter: $("${PYTHON_BIN}" -V 2>&1)"

for required in \
  requirements.txt schema.sql migrations/003_integrity_constraints.sql migrations/004_merge_hardening.sql \
  migrations/005_player_current_team.sql prop_alpha/__init__.py tests/test_merge_postgres.sql \
  tests/test_core.py tests/test_integrity.py tests/test_pipeline_concurrency.py tests/test_typing_contract.py; do
  [[ -f "${required}" ]] || die "repository is missing ${required}"
done

unpinned="$(grep -Ev '^[[:space:]]*(#|$)' requirements.txt \
  | grep -Ev '^[A-Za-z0-9._-]+==[^[:space:];]+([[:space:]]*;.*)?$' || true)"
[[ -z "${unpinned}" ]] || die "requirements.txt contains entries not pinned with '==': ${unpinned}"

if [[ "${PREFLIGHT_ONLY}" -eq 1 ]]; then
  log "preflight passed; nothing created or installed"
  exit 0
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

log "stage 5/6: test matrix (maths, integrity, typing contract, concurrency)"
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
  # Stage 6 drops the prop_alpha schema, so only the one known disposable database is accepted: a suffix
  # match would also accept a shared database that merely happens to end in _ci.
  [[ "${db_name}" == "${CI_DATABASE_NAME}" ]] \
    || die "refusing to reset database '${db_name}': the CI database must be named exactly ${CI_DATABASE_NAME}"
  server_version_num="$(psql_ci -At -c 'SHOW server_version_num')"
  (( server_version_num >= 150000 )) || die "PostgreSQL 15+ required; server_version_num=${server_version_num}"
  psql_ci -c 'DROP SCHEMA IF EXISTS prop_alpha CASCADE'
  psql_ci -f schema.sql
  psql_ci -c 'SELECT * FROM prop_alpha.merge_staging_player_feed()'
  # Behaviour on real rows (transfer, backfill, reject); the script rolls itself back.
  psql_ci -At -f tests/test_merge_postgres.sql >/dev/null
  # 005 on the current schema must be a no-op (idempotency).
  psql_ci -f migrations/005_player_current_team.sql
  psql_ci -c 'SELECT * FROM prop_alpha.merge_staging_player_feed()'
  # Upgrade path: 003 reinstalls the v3.2 zero-argument body (an overload of the v3.3 function); 004 must
  # drop it, so the zero-argument call in pipeline.MERGE_SQL resolves to the batched function again. 005
  # then installs the current body, which must behave exactly like a fresh install.
  psql_ci -f migrations/003_integrity_constraints.sql
  psql_ci -f migrations/004_merge_hardening.sql
  psql_ci -f migrations/005_player_current_team.sql
  psql_ci -c 'SELECT * FROM prop_alpha.merge_staging_player_feed()'
  psql_ci -c 'SELECT * FROM prop_alpha.merge_staging_player_feed(1)'
  psql_ci -At -f tests/test_merge_postgres.sql >/dev/null
  log "schema smoke test passed on ${db_name}"
else
  log "stage 6/6 skipped: PROP_ALPHA_CI_DATABASE_URL is unset, so schema.sql was not executed in this run"
fi

log "SUCCESS: environment built from pinned wheels; ${EXPECTED_TESTS}+ tests passed with none skipped"
