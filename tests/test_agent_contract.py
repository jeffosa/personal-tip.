"""Machine enforcement of AGENT.md: sanctioned handlers (1.3), pure-module isolation (1.4),
zero truncation (1.2), gate G1 (2.1), risk defaults and secrets (1.1 A3, A4)."""

from __future__ import annotations

import ast
import inspect
import re
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Callable

import pytest

from prop_alpha.ingestion import pipeline
from prop_alpha.modeling import engine
from prop_alpha.scanner import market_scanner

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "prop_alpha"

# AGENT.md 1.3 F2: (module, enclosing function, caught exception). Changing this set needs the AGENT.md
# table, the rationale and a human maintainer's approval in the same change.
SANCTIONED_HANDLERS: frozenset[tuple[str, str, str]] = frozenset({
    ("prop_alpha/ingestion/pipeline.py", "ConcurrentIngestionEngine._run_isolated", "Exception"),
    ("prop_alpha/ingestion/pipeline.py", "ConcurrentIngestionEngine.run_slate", "Exception"),
    ("prop_alpha/ingestion/pipeline.py", "ConcurrentIngestionEngine._with_transaction", "psycopg2.errors.QueryCanceled"),
    ("prop_alpha/ingestion/pipeline.py", "ConcurrentIngestionEngine._with_transaction", "RETRYABLE_DB_ERRORS"),
    ("prop_alpha/scanner/market_scanner.py", "InstitutionalAssetManager.allocate", "(ProjectionError, ValueError, FloatingPointError)"),
    # H6, added with the Telegram alerts at the maintainer's request; pending formal approval (RECONSTRUCTION.md Q6).
    ("prop_alpha/alerts/telegram.py", "TelegramNotifier._call", "requests.RequestException"),
    # H7, approved by the maintainer on 2026-10-03 (audit H4): one appearance per failure, recorded in `failures`.
    ("prop_alpha/evaluation/backtest.py", "walk_forward_over_probability", "(ProjectionError, ValueError, FloatingPointError)"),
    # H8, added on 2026-10-03 with the 4-hourly status report the maintainer asked for: an unreachable
    # database becomes a line in the report instead of silencing it.
    ("prop_alpha/alerts/status.py", "database_section", "psycopg2.Error"),
    # H9, added on 2026-10-03 with the 4-hourly opportunity scan the maintainer asked for: one fixture that
    # cannot be priced is recorded in the run summary and skipped; pending formal approval.
    ("prop_alpha/alerts/scan.py", "scan_fixture", "(ProjectionError, ValueError, FloatingPointError)"),
    # H10, added on 2026-10-03 with the odds collector: OddsPapi takes its key as a query parameter, and
    # requests quotes the URL in its messages, so transport errors are re-raised without it (as H6).
    ("prop_alpha/ingestion/pipeline.py", "VendorClient._get", "requests.RequestException"),
    # H11, added on 2026-10-03 after the audit rerun: an OddsPapi failure (403, outage, quota) becomes a line
    # in the scan summary instead of ending the run silently.
    ("prop_alpha/alerts/scan.py", "run_scan", "(requests.RequestException, QuotaExhaustedError, ValueError)"),
    # H12, approved by the maintainer on 2026-10-05: one failing bookmaker is recorded and skipped, the others' prices
    # are kept. Widened to ValueError on 2026-10-05 at the maintainer's request (production-readiness item 5): a
    # malformed reply from one bookmaker is dropped whole and recorded, instead of discarding every book's prices
    # (H11) or, for a KeyError/TypeError, crashing the run; pending formal approval.
    ("prop_alpha/alerts/scan.py", "collect_odds", "(requests.RequestException, VendorError, ValueError)"),
})

PURE_MODULES: tuple[str, ...] = (
    "prop_alpha/ingestion/transform.py",
    "prop_alpha/modeling/engine.py",
    "prop_alpha/scanner/market_scanner.py",
    "prop_alpha/calibration/fitting.py",
    "prop_alpha/evaluation/backtest.py",
    # Added with the high-value screen; same no-I/O and determinism rules as the AGENT P1 modules.
    "prop_alpha/scanner/value_screen.py",
    # Vendor adapter: response -> transform payload, same no-I/O and determinism rules.
    "prop_alpha/ingestion/sportmonks.py",
    "prop_alpha/ingestion/oddspapi.py",
    # Scan slate: projections and mock quotes for the 4-hourly opportunity scan.
    "prop_alpha/scanner/slate.py",
    # Bet-recording replies: parse "placed 3 @1.95 5" and resolve it against the replied-to message.
    "prop_alpha/alerts/replies.py",
    # League level: trailing positional rates and history re-scaled to today's level.
    "prop_alpha/modeling/league_level.py",
    # Gate 2 scoring: Brier skill, clustered bootstrap, calibration (randomness only from the caller's generator).
    "prop_alpha/evaluation/gate2.py",
)
IO_LIBRARIES = frozenset({
    "psycopg2", "requests", "urllib3", "socket", "http", "sqlite3", "threading", "concurrent", "asyncio", "subprocess",
})
HANDLE_NAMES = frozenset({"conn", "connection", "cur", "cursor", "pool", "session", "engine_conn"})
NONDETERMINISTIC_CALLS = re.compile(
    r"^(datetime\.(now|utcnow|today)|date\.today|time\.(time|monotonic|perf_counter)|os\.getenv|os\.environ\.get"
    r"|random\.\w+|np\.random\.\w+|numpy\.random\.\w+)$"
)


def _source(module: str) -> str:
    return (ROOT / module).read_text(encoding="utf-8")


def _package_modules() -> list[str]:
    return sorted(path.relative_to(ROOT).as_posix() for path in PACKAGE.rglob("*.py"))


def _qualified_functions(tree: ast.AST) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    found: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append((f"{prefix}{child.name}", child))
                visit(child, f"{prefix}{child.name}.")
            else:
                visit(child, prefix)

    visit(tree, "")
    return found


def _handlers(module: str, source: str) -> set[tuple[str, str, str]]:
    tree = ast.parse(source)
    handlers: set[tuple[str, str, str]] = set()
    covered: set[int] = set()
    for qualname, function in sorted(_qualified_functions(tree), key=lambda item: -item[0].count(".")):
        for node in ast.walk(function):
            if isinstance(node, ast.ExceptHandler) and id(node) not in covered:
                covered.add(id(node))
                caught = "<bare>" if node.type is None else ast.unparse(node.type)
                handlers.add((module, qualname.split(".<")[0], caught))
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and id(node) not in covered:
            handlers.add((module, "<module>", "<bare>" if node.type is None else ast.unparse(node.type)))
    return handlers


def _handler_violations(source: str) -> list[str]:
    problems: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ExceptHandler):
            if node.type is None:
                problems.append("bare except")
            if not any(isinstance(inner, (ast.Call, ast.Raise)) for stmt in node.body for inner in ast.walk(stmt)):
                problems.append(f"handler at line {node.lineno} neither records nor re-raises")
        if isinstance(node, ast.Attribute) and node.attr == "suppress":
            problems.append("contextlib.suppress")
    return problems


def _io_imports(source: str) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    return imported & IO_LIBRARIES


def _handle_parameters(source: str) -> list[str]:
    problems: list[str] = []
    for qualname, function in _qualified_functions(ast.parse(source)):
        arguments = function.args.posonlyargs + function.args.args + function.args.kwonlyargs
        problems.extend(f"{qualname}({a.arg})" for a in arguments if a.arg in HANDLE_NAMES)
    return problems


def _nondeterministic_calls(source: str) -> list[str]:
    problems: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and NONDETERMINISTIC_CALLS.match(ast.unparse(node.func)):
            problems.append(ast.unparse(node.func))
        if isinstance(node, ast.Attribute) and ast.unparse(node) == "os.environ":
            problems.append("os.environ")
    return problems


def _is_square(node: ast.AST) -> bool:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
        return isinstance(node.right, ast.Constant) and node.right.value == 2
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        return ast.dump(node.left) == ast.dump(node.right)
    if isinstance(node, ast.Call):
        name = ast.unparse(node.func).split(".")[-1]
        return name in {"outer", "square"}
    return False


def _raw_moment_subtractions(source: str) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub) and _is_square(node.right)
    ]


_PLACEHOLDER_TEXT = re.compile(
    r"\b(" + "TO" + "DO|" + "FIX" + "ME|" + "X" + r"XX)\b"
    r"|#\s*\.\.\.\s*$|--\s*\.\.\.\s*$|#\s*rest unchanged|#\s*implement remainder here",
    re.M,
)


def _placeholder_violations(source: str, is_python: bool) -> list[str]:
    problems = [match.group(0) for match in _PLACEHOLDER_TEXT.finditer(source)]
    if not is_python:
        return problems
    tree = ast.parse(source)
    protocol_functions: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and any(ast.unparse(base).endswith("Protocol") for base in node.bases):
            protocol_functions.update(id(child) for child in node.body)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and id(node) not in protocol_functions:
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                body = body[1:]
            if len(body) == 1 and (
                isinstance(body[0], ast.Pass)
                or (isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and body[0].value.value is Ellipsis)
            ):
                problems.append(f"{node.name}: placeholder body")
        if isinstance(node, ast.Raise) and node.exc is not None and "NotImplementedError" in ast.unparse(node.exc):
            problems.append("raise NotImplementedError")
    return problems


def _python_placeholders(source: str) -> list[str]:
    return _placeholder_violations(source, True)


def _text_placeholders(source: str) -> list[str]:
    return _placeholder_violations(source, False)


# ---------------- 1.3 fail-closed handlers ----------------
def test_only_sanctioned_exception_handlers_exist() -> None:
    found: set[tuple[str, str, str]] = set()
    for module in _package_modules():
        found |= _handlers(module, _source(module))
    assert found == SANCTIONED_HANDLERS


def test_every_handler_records_or_reraises() -> None:
    for module in _package_modules():
        assert _handler_violations(_source(module)) == [], module


def test_statement_timeout_is_caught_before_retryable_errors() -> None:
    function = next(f for name, f in _qualified_functions(ast.parse(_source("prop_alpha/ingestion/pipeline.py")))
                    if name == "ConcurrentIngestionEngine._with_transaction")
    tries = [node for node in ast.walk(function) if isinstance(node, ast.Try) and node.handlers]
    assert len(tries) == 1
    caught = [ast.unparse(handler.type) for handler in tries[0].handlers if handler.type is not None]
    assert caught == ["psycopg2.errors.QueryCanceled", "RETRYABLE_DB_ERRORS"]
    assert isinstance(tries[0].handlers[0].body[-1], ast.Raise)
    assert any(ast.unparse(stmt).startswith("self.pool.putconn") for stmt in tries[0].finalbody)


def test_cli_exit_code_signals_an_incomplete_slate(monkeypatch: pytest.MonkeyPatch) -> None:
    class StubEngine:
        JOBS = ("stats", "lineups")

        def __init__(self, *args: object, **kwargs: object) -> None:
            self.report = pipeline.SlateReport(job="stats", timed_out=["FX-9"])

        def __enter__(self) -> StubEngine:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def run_slate(self, *args: object, **kwargs: object) -> pipeline.SlateReport:
            return self.report

    class StubPool:
        def closeall(self) -> None:
            return None

    # The generic vendor takes its key from these variables; the default (sportmonks) would need a real token.
    for name, value in (("DATA_VENDOR", "generic"), ("DATA_VENDOR_BASE_URL", "https://vendor.invalid"),
                        ("DATA_VENDOR_API_KEY", "unused"),
                        ("PROP_ALPHA_DB_DSN", "postgresql://unused"), ("TARGET_FIXTURE_UIDS", "FX-9")):
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(pipeline, "build_pool", lambda dsn, workers: StubPool())
    monkeypatch.setattr(pipeline, "ConcurrentIngestionEngine", StubEngine)
    assert pipeline.main(["stats"]) == 1


# ---------------- 1.4 pure transform isolation ----------------
@pytest.mark.parametrize("module", PURE_MODULES)
def test_pure_modules_import_no_io_libraries(module: str) -> None:
    assert _io_imports(_source(module)) == set()


@pytest.mark.parametrize("module", PURE_MODULES)
def test_pure_functions_accept_no_database_or_session_handles(module: str) -> None:
    assert _handle_parameters(_source(module)) == []


@pytest.mark.parametrize("module", PURE_MODULES)
def test_pure_modules_read_no_clock_environment_or_random_state(module: str) -> None:
    assert _nondeterministic_calls(_source(module)) == []


def test_with_transaction_is_the_single_connection_acquisition_site() -> None:
    sites: list[tuple[str, str, str]] = []
    for module in _package_modules():
        for qualname, function in _qualified_functions(ast.parse(_source(module))):
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("getconn", "putconn"):
                    sites.append((module, qualname, node.func.attr))
    assert sorted(set(sites)) == [
        ("prop_alpha/ingestion/pipeline.py", "ConcurrentIngestionEngine._with_transaction", "getconn"),
        ("prop_alpha/ingestion/pipeline.py", "ConcurrentIngestionEngine._with_transaction", "putconn"),
    ]
    assert len(sites) == 2


@pytest.mark.parametrize(
    ("task", "fetch", "parsers"),
    [
        ("_stats_task", "get_fixture_feed", ("parse_fixture_payload", "build_staging_rows")),
        ("_lineup_task", "get_lineups", ("parse_lineup_payload",)),
    ],
)
def test_fetch_and_parse_complete_before_the_transaction(task: str, fetch: str, parsers: tuple[str, ...]) -> None:
    tree = ast.parse(_source("prop_alpha/ingestion/pipeline.py"))
    function = next(f for name, f in _qualified_functions(tree) if name == f"ConcurrentIngestionEngine.{task}")
    calls = {ast.unparse(node.func).split(".")[-1]: node.lineno for node in ast.walk(function) if isinstance(node, ast.Call)}
    transaction_line = calls["_with_transaction"]
    for name in (fetch, *parsers):
        assert calls[name] < transaction_line, f"{name} must run before _with_transaction"
    closure = next(f for f in function.body if isinstance(f, ast.FunctionDef))
    closure_calls = {ast.unparse(node.func).split(".")[-1] for node in ast.walk(closure) if isinstance(node, ast.Call)}
    assert closure_calls.isdisjoint({fetch, *parsers, "get_json"})


def test_referee_job_defers_its_database_driver_import() -> None:
    tree = ast.parse(_source("prop_alpha/calibration/referee_job.py"))
    top_level = {alias.name.split(".")[0] for node in tree.body if isinstance(node, ast.Import) for alias in node.names}
    top_level |= {node.module.split(".")[0] for node in tree.body if isinstance(node, ast.ImportFrom) and node.module}
    assert "psycopg2" not in top_level
    run = next(f for name, f in _qualified_functions(tree) if name == "run_referee_job")
    assert any(isinstance(node, ast.Import) and node.names[0].name == "psycopg2" for node in ast.walk(run))
    script = (
        "import sys; sys.modules['psycopg2'] = None\n"
        "from prop_alpha.calibration.referee_job import FixtureDiscipline, compute_referee_coefficients\n"
        "print(len(compute_referee_coefficients([FixtureDiscipline(1, 1, 1, 5, 6, 1, 0)])))\n"
    )
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"


# ---------------- 1.2 zero truncation ----------------
def test_no_placeholders_or_truncation_markers() -> None:
    files = [*PACKAGE.rglob("*.py"), *(ROOT / "tests").glob("*.py"), ROOT / "schema.sql",
             *(ROOT / "migrations").glob("*.sql"), ROOT / "ci" / "bootstrap.sh"]
    problems = {
        path.relative_to(ROOT).as_posix(): found
        for path in files
        if (found := _placeholder_violations(path.read_text(encoding="utf-8"), path.suffix == ".py"))
    }
    assert problems == {}


# ---------------- 2.1 gate G1 ----------------
def test_engine_contains_no_raw_moment_subtraction() -> None:
    assert _raw_moment_subtractions(_source("prop_alpha/modeling/engine.py")) == []


def test_return_moments_documents_its_condition_number_bound() -> None:
    docstring = inspect.getdoc(market_scanner.return_moments) or ""
    assert "kappa" in docstring and "G1" in docstring


# ---------------- 1.1 A3 safeguards and A4 secrets ----------------
def test_risk_defaults_and_numerical_bounds_are_unchanged() -> None:
    defaults = {
        name: parameter.default
        for name, parameter in inspect.signature(market_scanner.InstitutionalAssetManager.__init__).parameters.items()
        if parameter.default is not inspect.Parameter.empty
    }
    assert defaults == {
        "risk_aversion": 2.0,
        "alpha_threshold": 0.02,
        "max_position_fraction": 0.02,
        "max_fixture_fraction": 0.05,
        "max_total_fraction": 0.20,
        "max_quote_age": timedelta(minutes=5),
        "min_stake": 1.0,
    }
    # Open positions now enter allocate() as a required ExposureBook (remediation S1), not a constructor default.
    floor = inspect.signature(market_scanner.InstitutionalAssetManager.__init__).parameters["model_risk_loss_floor"]
    assert floor.default is inspect.Parameter.empty and floor.kind is inspect.Parameter.KEYWORD_ONLY
    assert market_scanner.MODEL_RISK_LOSS_FLOOR == 0.03  # maintainer decision 1, 2026-10-01
    assert (engine.MIN_RELATIVE_VARIANCE, engine.MAX_RELATIVE_VARIANCE, engine.MAX_MATCH_MINUTES) == (1e-6, 1e3, 130.0)
    assert market_scanner.DEGENERATE_LOSS_PROBABILITY == 1e-6


_SECRET_PATTERNS = re.compile(
    r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"            # Telegram bot token
    r"|sk_(?:prod|live)_[A-Za-z0-9]+"            # live API keys
    r"|postgres(?:ql)?://[^\s:/@]+:[^\s@$][^\s@]*@"  # DSN with an inline password (not ${VAR})
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)
_SKIPPED_PARTS = frozenset({".git", ".venv", ".venv-1", ".venv-ci", "ci-artifacts", "__pycache__", ".pytest_cache"})


def _secret_hits(text: str) -> list[str]:
    return _SECRET_PATTERNS.findall(text)


def test_no_secrets_in_repository_files() -> None:
    hits: dict[str, int] = {}
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if not path.is_file() or _SKIPPED_PARTS & set(relative.parts) or relative.name == ".env":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if found := _secret_hits(text):
            hits[relative.as_posix()] = len(found)
    assert hits == {}


# ---------------- the detectors bite ----------------
_TOKEN_SHAPED = "1234567890" + ":" + "A" * 35


@pytest.mark.parametrize(
    ("detector", "violation"),
    [
        (_handler_violations, "try:\n    x = 1\nexcept ValueError:\n    pass\n"),
        (_handler_violations, "try:\n    x = 1\nexcept:\n    raise\n"),
        (_io_imports, "import requests\n"),
        (_handle_parameters, "def parse(payload: dict, cur: object) -> None:\n    return None\n"),
        (_nondeterministic_calls, "from datetime import datetime\nstamp = datetime.now()\n"),
        (_raw_moment_subtractions, "variance = second_moment - mean**2\n"),
        (_raw_moment_subtractions, "cov = second - np.outer(mu, mu)\n"),
        (_python_placeholders, "def f() -> None:\n    pass\n"),
        (_text_placeholders, "-- " + "TO" + "DO: add the reject predicate\n"),
        (_secret_hits, f"TELEGRAM_BOT_TOKEN={_TOKEN_SHAPED}\n"),
        (_secret_hits, "dsn = 'postgresql://admin:" + "hunter2@db.internal/prod'\n"),
    ],
    # Named cases: the default ids would print the fake token and placeholder in `pytest -v` output, which then
    # trips the secrets and placeholder scans in any report that captures it.
    ids=["bare-pass-handler", "bare-except", "io-import", "cursor-parameter", "clock-read", "raw-moment",
         "outer-moment", "pass-body", "sql-placeholder", "telegram-token", "dsn-password"],
)
def test_contract_detectors_flag_deliberate_violations(detector: Callable[[str], object], violation: str) -> None:
    assert detector(violation)
