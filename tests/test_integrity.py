from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Sequence, cast

import numpy as np
import pytest

from prop_alpha.ingestion.transform import (
    STAGING_COLUMNS,
    STATS_COLUMNS,
    DEFAULT_KAPPAS,
    build_staging_rows,
    build_stats_row,
    parse_fixture_payload,
)
from prop_alpha.calibration import referee_job
from prop_alpha.ingestion import pipeline
from prop_alpha.modeling.engine import GammaPosterior, QuantEngine, validate_exposure_vector

PayloadFactory = Callable[..., dict[str, Any]]


# ---------------- exposure validation (ARCHITECTURE-ESSENTIALS 2.3) ----------------
def test_valid_vectors_are_returned_typed() -> None:
    minutes, counts = validate_exposure_vector([90.0, 0.0], [3, 0])
    assert minutes.dtype == np.float64
    assert counts.dtype == np.int64
    np.testing.assert_array_equal(minutes, np.array([90.0, 0.0]))
    np.testing.assert_array_equal(counts, np.array([3, 0]))


@pytest.mark.parametrize(
    ("minutes", "counts"),
    [
        ([90.0], [2.7]),
        ([float("nan")], [1]),
        ([float("inf")], [1]),
        ([90.0], [float("nan")]),
        ([90.0], [float("inf")]),
        ([90.0], [True]),
        ([0.0], [3]),
        ([131.0], [1]),
        ([90.0, 45.0], [1]),
        ([], []),
        ([90.0], [-1]),
    ],
)
def test_polluted_vectors_are_rejected(minutes: object, counts: object) -> None:
    with pytest.raises(ValueError):
        validate_exposure_vector(cast(Sequence[float], minutes), cast(Sequence[int], counts))


def test_gamma_update_uses_validation_and_allows_prior_only_history() -> None:
    prior = GammaPosterior(alpha=6.0, beta=3.0)
    posterior = QuantEngine.gamma_poisson_update(prior, [], [])
    assert posterior == prior
    updated = QuantEngine.gamma_poisson_update(prior, [2, 1], [90.0, 45.0])
    assert updated.alpha == 9.0
    assert updated.beta == 4.5
    with pytest.raises(ValueError):
        QuantEngine.gamma_poisson_update(prior, [1], [0.0])


def test_zero_minute_vector_contributes_nothing_to_beta() -> None:
    prior = GammaPosterior(alpha=6.0, beta=3.0)
    assert QuantEngine.gamma_poisson_update(prior, [0], [0.0]) == prior


# ---------------- L1 transform parity with the core CHECK constraints ----------------
def test_transform_rejects_minute_off_before_minute_on(fixture_payload: PayloadFactory) -> None:
    payload = fixture_payload()
    payload["players"][1].update({"minute_on": 70, "minute_off": 60})
    with pytest.raises(ValueError, match="minute_off precedes minute_on"):
        parse_fixture_payload(payload)


def test_transform_rejects_minutes_above_match_maximum(fixture_payload: PayloadFactory) -> None:
    payload = fixture_payload()
    payload["players"][0]["stats"]["minutes"] = 131
    with pytest.raises(ValueError, match="minutes_played exceeds"):
        parse_fixture_payload(payload)


def test_transform_accepts_extra_time_at_the_bound(fixture_payload: PayloadFactory) -> None:
    payload = fixture_payload()
    payload["players"][0]["stats"]["minutes"] = 130
    assert parse_fixture_payload(payload).players[0].minutes_played == 130


@pytest.mark.parametrize("possession", [101.0, -0.5])
def test_transform_rejects_possession_outside_bounds(fixture_payload: PayloadFactory, possession: float) -> None:
    payload = fixture_payload()
    payload["teams"]["T-HOME"]["possession_pct"] = possession
    with pytest.raises(ValueError, match="possession out of range"):
        parse_fixture_payload(payload)


@pytest.mark.parametrize("possession", [0.0, 100.0])
def test_transform_admits_possession_at_bounds(fixture_payload: PayloadFactory, possession: float) -> None:
    payload = fixture_payload()
    payload["teams"]["T-HOME"]["possession_pct"] = possession
    record = parse_fixture_payload(payload).players[0]
    assert record.team_possession_pct == possession
    assert all(np.isfinite(value) for value in record.derived_features(DEFAULT_KAPPAS)[:3])


def test_renamed_market_metric_fails_the_fixture(fixture_payload: PayloadFactory) -> None:
    """I1: `fouls_committed` renamed to `fouls` used to parse as 90 minutes and 0 fouls."""
    payload = fixture_payload()
    stats = payload["players"][0]["stats"]
    stats["fouls"] = stats.pop("fouls_committed")
    with pytest.raises(ValueError, match="'fouls_committed' missing"):
        parse_fixture_payload(payload)


@pytest.mark.parametrize("metric", ["tackles_won", "fouls_committed", "fouls_drawn", "yellow_cards", "red_cards",
                                    "tackles_attempted", "minutes"])
def test_every_market_metric_is_required_on_the_pitch(fixture_payload: PayloadFactory, metric: str) -> None:
    payload = fixture_payload()
    del payload["players"][0]["stats"][metric]
    with pytest.raises(ValueError, match="missing"):
        parse_fixture_payload(payload)


@pytest.mark.parametrize("started", ["false", "true", 0, 1, None])
def test_started_must_be_a_json_boolean(fixture_payload: PayloadFactory, started: object) -> None:
    """I1: bool("false") is True, which skewed avg_start and every starter's expected minutes."""
    payload = fixture_payload()
    payload["players"][1]["started"] = started
    with pytest.raises(ValueError, match="JSON boolean"):
        parse_fixture_payload(payload)


@pytest.mark.parametrize("value", ["2", 2.5, True, None])
def test_market_metric_must_be_an_integer(fixture_payload: PayloadFactory, value: object) -> None:
    payload = fixture_payload()
    payload["players"][0]["stats"]["fouls_committed"] = value
    with pytest.raises(ValueError, match="not an integer"):
        parse_fixture_payload(payload)


def test_missing_stats_block_fails_and_unused_substitute_may_omit_metrics(fixture_payload: PayloadFactory) -> None:
    payload = fixture_payload()
    del payload["players"][0]["stats"]
    with pytest.raises(ValueError, match="'stats' object missing"):
        parse_fixture_payload(payload)
    unused = fixture_payload()
    unused["players"][1]["stats"] = {"minutes": 0}
    record = parse_fixture_payload(unused).players[1]
    assert (record.minutes_played, record.fouls_committed, record.tackles_attempted) == (0, 0, 0)
    integral = fixture_payload()
    integral["players"][0]["stats"]["fouls_committed"] = 2.0
    assert parse_fixture_payload(integral).players[0].fouls_committed == 2


def test_lineup_parse_validates_per_player_bench_probabilities() -> None:
    from conftest import make_lineup_payload

    from prop_alpha.ingestion.transform import parse_lineup_payload

    payload = make_lineup_payload(n_bench=1)
    bench = payload["lineups"]["T-HOME"]["bench"][0]
    entries = {e.vendor_player_uid: e for e in parse_lineup_payload(payload, 0.35, {bench: 0.6})}
    assert entries[bench].p_sub_appearance == 0.6
    assert entries[payload["lineups"]["T-AWAY"]["bench"][0]].p_sub_appearance == 0.35
    for bad in (1.5, -0.1, True):
        with pytest.raises(ValueError):
            parse_lineup_payload(payload, 0.35, {bench: bad})  # type: ignore[dict-item]


def test_staging_rows_have_header_and_full_width(fixture_payload: PayloadFactory) -> None:
    parsed = parse_fixture_payload(fixture_payload(n_players=3))
    rows = build_staging_rows(parsed, DEFAULT_KAPPAS, "00000000-0000-0000-0000-000000000001")
    assert len(STAGING_COLUMNS) == 41
    assert len(rows) == 1 + len(parsed.players)
    assert all(len(row) == len(STAGING_COLUMNS) for row in rows)
    header = dict(zip(STAGING_COLUMNS, rows[0]))
    assert header["vendor_fixture_uid"] == "FX-1"
    assert all(header[column] is None for column in STAGING_COLUMNS[11:])
    first_player = dict(zip(STAGING_COLUMNS, rows[1]))
    assert first_player["vendor_player_uid"] == parsed.players[0].player_uid
    assert first_player["minutes_played"] == parsed.players[0].minutes_played


def test_stats_row_matches_stats_columns(fixture_payload: PayloadFactory) -> None:
    parsed = parse_fixture_payload(fixture_payload())
    row = build_stats_row(parsed.players[0], 1, 2, 3, DEFAULT_KAPPAS)
    assert len(row) == len(STATS_COLUMNS) == 29
    by_column = dict(zip(STATS_COLUMNS, row))
    assert by_column["tackles_attempted"] == parsed.players[0].tackles_attempted
    assert by_column["dribbled_past_rate"] == pytest.approx(1 / 5)


def test_staging_player_columns_mirror_stats_columns() -> None:
    staging_player_columns = set(STAGING_COLUMNS[11:]) - {"vendor_player_uid", "player_name", "player_position", "team_uid"}
    stats_payload_columns = set(STATS_COLUMNS) - {"fixture_id", "player_id", "team_id"}
    assert staging_player_columns == stats_payload_columns


# ---------------- referee job: lock first, ordered update ----------------
class _RecordingCursor:
    def __init__(self, log: list[tuple[str, object]]) -> None:
        self.log = log
        self.connection = self
        self.encoding = "UTF8"

    def __enter__(self) -> _RecordingCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: object, params: object = None) -> None:
        text = sql.decode("utf-8") if isinstance(sql, bytes) else str(sql)
        self.log.append((text, params))

    def mogrify(self, template: object, args: tuple[object, ...]) -> bytes:
        self.log.append(("row", args))
        return repr(args).encode("utf-8")

    def fetchall(self) -> list[tuple[int, ...]]:
        return [(2, 7, 10 + f, 9, 11, 2, 0) for f in range(3)] + [(1, 7, 20 + f, 14, 16, 4, 0) for f in range(3)]


class _RecordingConnection:
    def __init__(self) -> None:
        self.log: list[tuple[str, object]] = []
        self.closed = False

    def __enter__(self) -> _RecordingConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def cursor(self) -> _RecordingCursor:
        return _RecordingCursor(self.log)

    def close(self) -> None:
        self.closed = True


def test_referee_job_takes_merge_lock_before_reading_and_updates_in_id_order(monkeypatch: pytest.MonkeyPatch) -> None:
    import psycopg2

    connection = _RecordingConnection()
    options: list[str] = []
    monkeypatch.setattr(psycopg2, "connect", lambda dsn, **kwargs: options.append(kwargs.get("options", "")) or connection)
    assert referee_job.run_referee_job("postgresql://unused", league_id=7) == 2
    statements = [entry for entry in connection.log if entry[0] != "row"]
    assert statements[0] == (referee_job.ADVISORY_LOCK_SQL, (referee_job.MERGE_LOCK_KEY,))
    assert "player_match_stats" in statements[1][0] and statements[1][1] == (7, 730)
    assert "UPDATE prop_alpha.referees AS r" in statements[2][0]
    rows = [args for kind, args in connection.log if kind == "row"]
    assert [row[0] for row in rows] == [1, 2]
    # Audit H2: bounded waits while holding the merge lock, and only this league's referees are written.
    assert options == [referee_job.CONNECTION_OPTIONS]
    assert "lock_timeout" in options[0] and "statement_timeout" in options[0]
    assert all(row[1] == 7 for row in rows) and "AND r.league_id = v.league_id" in referee_job.UPDATE_SQL
    assert connection.closed


# ---------------- SQL structure (text checks; ci/bootstrap.sh stage 6 executes the SQL on PostgreSQL) ----------------
ROOT = Path(__file__).resolve().parents[1]
SCHEMA = (ROOT / "schema.sql").read_text(encoding="utf-8")
MIGRATION_002 = (ROOT / "migrations" / "002_staging_and_indexes.sql").read_text(encoding="utf-8")
MIGRATION_003 = (ROOT / "migrations" / "003_integrity_constraints.sql").read_text(encoding="utf-8")
MIGRATION_004 = (ROOT / "migrations" / "004_merge_hardening.sql").read_text(encoding="utf-8")
MIGRATION_005 = (ROOT / "migrations" / "005_player_current_team.sql").read_text(encoding="utf-8")
MIGRATION_006 = (ROOT / "migrations" / "006_player_attributes_by_kickoff.sql").read_text(encoding="utf-8")
MIGRATION_007 = (ROOT / "migrations" / "007_match_position.sql").read_text(encoding="utf-8")
MERGE_SOURCES = {"schema.sql": SCHEMA, "003": MIGRATION_003, "004": MIGRATION_004, "005": MIGRATION_005,
                 "006": MIGRATION_006, "007": MIGRATION_007}
CORE_CONSTRAINTS = (
    "chk_possession_bounds", "chk_minute_logic", "chk_tackles_won_le_attempted", "chk_duels_won_le_total",
)
ODDS_CONSTRAINTS = ("chk_price_realism", "chk_line_increment")

# L2 reject predicate fragments required for each core CHECK (ARCHITECTURE-ESSENTIALS 3.1 parity table).
REJECT_PREDICATES = {
    "chk_possession_bounds": ("c.team_possession_pct NOT BETWEEN 0 AND 100",),
    "chk_minute_logic": ("c.minute_off < c.minute_on", "c.minutes_played > 130"),
    "chk_tackles_won_le_attempted": ("c.tackles_won > c.tackles_attempted",),
    "chk_duels_won_le_total": ("c.duels_won > c.duels_total",),
    "yellow_cards BETWEEN 0 AND 2": ("c.yellow_cards NOT BETWEEN 0 AND 2",),
    "red_cards BETWEEN 0 AND 1": ("c.red_cards NOT BETWEEN 0 AND 1",),
    "minutes_played >= 0": ("c.minutes_played < 0",),
    "minute_on >= 0": ("c.minute_on < 0",),
    "minute_off >= 0": ("c.minute_off < 0",),
}

# D1 (migration 004): NULL market metrics on a player who was on the pitch are rejected, never COALESCEd to 0.
NULL_METRIC_PREDICATES = (
    "OR c.minutes_played IS NULL",
    "c.minutes_played > 0 AND (",
    "c.tackles_attempted IS NULL", "c.tackles_won IS NULL", "c.fouls_committed   IS NULL",
    "c.fouls_drawn IS NULL", "c.yellow_cards      IS NULL", "c.red_cards   IS NULL",
)


def _strip_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def _merge_function(sql: str) -> str:
    start = sql.index("CREATE OR REPLACE FUNCTION prop_alpha.merge_staging_player_feed(")
    return sql[start:sql.index("$$;", start) + 3]


def _player_reject_block(function: str) -> str:
    end = function.index("'player row violates core constraints'")
    return function[function.rindex("WITH bad AS", 0, end):end]


def _table_columns(sql: str, table: str) -> list[str]:
    pattern = rf"CREATE (?:UNLOGGED )?TABLE (?:IF NOT EXISTS )?(?:prop_alpha\.)?{table} \((.*?)\n\)"
    match = re.search(pattern, sql, re.S)
    assert match, f"table {table} not found"
    columns = []
    for line in _strip_comments(match.group(1)).splitlines():
        token = line.strip().split(" ")[0] if line.strip() else ""
        if token and token.upper() not in ("CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK") and not token.startswith("("):
            columns.append(token)
    return columns


def _merge_stats_insert_columns(function: str) -> list[str]:
    match = re.search(r"INSERT INTO prop_alpha\.player_match_stats AS s \((.*?)\)\s*SELECT", function, re.S)
    assert match
    return [column.strip() for column in match.group(1).replace("\n", " ").split(",")]


@pytest.mark.parametrize("source", ["schema.sql", "003", "004", "005", "006", "007"])
def test_merge_rejects_every_row_a_core_constraint_would_refuse(source: str) -> None:
    block = _player_reject_block(_merge_function(MERGE_SOURCES[source]))
    for constraint, fragments in REJECT_PREDICATES.items():
        for fragment in fragments:
            assert fragment in block, f"{constraint}: missing L2 predicate {fragment!r}"
    for constraint in CORE_CONSTRAINTS:
        assert f"CONSTRAINT {constraint}" in SCHEMA


@pytest.mark.parametrize("source", ["schema.sql", "004", "005", "006", "007"])
def test_merge_rejects_null_market_metrics(source: str) -> None:
    block = _player_reject_block(_merge_function(MERGE_SOURCES[source]))
    for fragment in NULL_METRIC_PREDICATES:
        assert fragment in block, f"missing D1 predicate {fragment!r}"
    assert "c.minutes_played IS NULL" not in _player_reject_block(_merge_function(MIGRATION_003))


def test_merge_function_identical_in_schema_and_latest_migration() -> None:
    assert _merge_function(SCHEMA) == _merge_function(MIGRATION_007)
    assert _merge_function(MIGRATION_006) != _merge_function(MIGRATION_007)
    assert _merge_function(MIGRATION_005) != _merge_function(MIGRATION_006)
    assert _merge_function(MIGRATION_004) != _merge_function(MIGRATION_005)
    assert _merge_function(MIGRATION_003) != _merge_function(MIGRATION_004)


CURRENT_TEAM_FROM_LATEST_KICKOFF = (
    "SET current_team_id = latest.team_id",
    "ORDER BY s.player_id, f.kickoff_at DESC, f.fixture_id DESC",
    "AND pl.current_team_id IS DISTINCT FROM latest.team_id",
)


@pytest.mark.parametrize("source", ["schema.sql", "005", "006", "007"])
def test_merge_moves_player_to_team_of_latest_fixture(source: str) -> None:
    function = _merge_function(MERGE_SOURCES[source])
    for fragment in CURRENT_TEAM_FROM_LATEST_KICKOFF:
        assert fragment in function, f"missing current-team fragment {fragment!r}"
    # After the stats upsert, so the player's newest row is already visible.
    assert function.index("INSERT INTO prop_alpha.player_match_stats") < function.index("UPDATE prop_alpha.players")


@pytest.mark.parametrize("source", ["004", "005"])
def test_migration_replaces_the_zero_argument_function_in_one_transaction(source: str) -> None:
    body = _strip_comments(MERGE_SOURCES[source])
    statements = [line.strip() for line in body.splitlines() if line.strip()]
    assert statements[:3] == [
        "SET lock_timeout = '5s';", "BEGIN;", "DROP FUNCTION IF EXISTS prop_alpha.merge_staging_player_feed();",
    ]
    assert statements[3] == (
        "CREATE OR REPLACE FUNCTION prop_alpha.merge_staging_player_feed(p_max_batches integer DEFAULT 200)"
    )
    assert statements[-2:] == ["COMMIT;", "RESET lock_timeout;"]
    for statement in ("TRUNCATE", "VACUUM FULL", "CLUSTER", "ALTER COLUMN"):
        assert statement not in body
    assert "merge_staging_player_feed()" in pipeline.MERGE_SQL


def test_superseded_002_merge_lacks_minute_logic_predicates() -> None:
    old = _player_reject_block(_merge_function(MIGRATION_002))
    assert "c.minutes_played > 130" not in old and "c.minute_off < c.minute_on" not in old
    assert _merge_function(MIGRATION_002) != _merge_function(SCHEMA)


def test_migration_adds_not_valid_then_validates() -> None:
    begin, commit = MIGRATION_003.index("\nBEGIN;"), MIGRATION_003.index("\nCOMMIT;")
    transaction, after = MIGRATION_003[begin:commit], MIGRATION_003[commit:]
    assert "ADD CONSTRAINT %I %s NOT VALID" in transaction
    for constraint in CORE_CONSTRAINTS + ODDS_CONSTRAINTS:
        assert f"'{constraint}'" in transaction
        assert f"VALIDATE CONSTRAINT {constraint};" in after
        assert f"VALIDATE CONSTRAINT {constraint}" not in transaction
    last_validate = after.rindex("VALIDATE CONSTRAINT")
    drops = [m.start() for m in re.finditer(r"DROP CONSTRAINT IF EXISTS \w+", after)]
    assert drops and all(position > last_validate for position in drops)


def test_migration_adds_constraints_without_scanning_and_is_idempotent() -> None:
    body = _strip_comments(MIGRATION_003)
    assert body.lstrip().startswith("SET lock_timeout = '5s';")
    assert "SELECT 1 FROM pg_constraint WHERE conrelid = spec.tbl AND conname = spec.conname" in body
    assert "IF NOT EXISTS (" in body
    assert "CREATE OR REPLACE FUNCTION" in body
    assert not re.search(r"ADD CONSTRAINT(?![^\n]*NOT VALID)", body)
    assert "RESET lock_timeout;" in body
    for statement in ("TRUNCATE", "VACUUM FULL", "CLUSTER", "ALTER COLUMN"):
        assert statement not in body


def test_index_changes_in_002_are_concurrent_and_outside_the_transaction() -> None:
    after_commit = MIGRATION_002[MIGRATION_002.index("\nCOMMIT;"):]
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_odds_fixture_captured" in after_commit
    assert "DROP INDEX CONCURRENTLY IF EXISTS prop_alpha.idx_odds_captured_brin" in after_commit


def test_referee_job_shares_merge_advisory_lock_key() -> None:
    for sql in (SCHEMA, MIGRATION_003, MIGRATION_004, MIGRATION_005):
        keys = re.findall(r"pg_advisory_xact_lock\(hashtext\('([^']+)'\)\)", sql)
        assert keys == [referee_job.MERGE_LOCK_KEY]
    # 006 also takes the lock for its one-time players repair.
    keys = re.findall(r"pg_advisory_xact_lock\(hashtext\('([^']+)'\)\)", MIGRATION_006)
    assert keys == [referee_job.MERGE_LOCK_KEY] * 2


@pytest.mark.parametrize("source", ["schema.sql", "003", "004", "005", "006"])
def test_merge_drains_by_single_delete_returning_after_taking_the_lock(source: str) -> None:
    function = _merge_function(MERGE_SOURCES[source])
    body = _strip_comments(function[function.index("\nBEGIN\n"):])
    statements = [line.strip() for line in body.splitlines() if line.strip()]
    assert statements[0] == "BEGIN"
    assert statements[1] == "PERFORM pg_advisory_xact_lock(hashtext('prop_alpha.merge_staging_player_feed'));"
    if source == "003":
        drain = (
            "WITH drained AS (\n        DELETE FROM prop_alpha.staging_player_feed RETURNING *\n    )\n"
            "    INSERT INTO claimed_feed SELECT * FROM drained;"
        )
    else:
        # D2: whole batches, oldest first, at most p_max_batches per call; still one claiming statement.
        drain = (
            "    WITH oldest AS (\n"
            "        SELECT s.batch_id\n"
            "        FROM prop_alpha.staging_player_feed s\n"
            "        GROUP BY s.batch_id\n"
            "        ORDER BY min(s.staged_at), s.batch_id\n"
            "        LIMIT p_max_batches\n"
            "    ), drained AS (\n"
            "        DELETE FROM prop_alpha.staging_player_feed s\n"
            "        USING oldest o\n"
            "        WHERE s.batch_id = o.batch_id\n"
            "        RETURNING s.*\n"
            "    )\n"
            "    INSERT INTO claimed_feed SELECT * FROM drained;"
        )
    assert drain in function
    assert function.count("DELETE FROM prop_alpha.staging_player_feed") == 1
    assert "RETURNS TABLE (fixtures_merged integer, player_rows_merged integer, rows_rejected integer)" in function
    for column in ("fixtures_merged", "player_rows_merged", "rows_rejected"):
        assert column in pipeline.MERGE_SQL


def test_no_truncate_in_any_sql() -> None:
    for sql in (SCHEMA, MIGRATION_002, MIGRATION_003, MIGRATION_004, MIGRATION_005, MIGRATION_006):
        assert "TRUNCATE" not in _strip_comments(sql).upper()


def test_merge_upserts_order_by_conflict_key_and_keep_latest_row() -> None:
    function = _merge_function(SCHEMA)
    assert "ORDER BY c.referee_uid, c.staged_at DESC" in function
    assert "ORDER BY cf.vendor_fixture_uid, cf.staged_at DESC" in function
    # Players: latest match by kickoff wins (migration 006), the staged order only breaks exact ties.
    assert "ORDER BY c.vendor_player_uid, c.kickoff_at DESC, c.vendor_fixture_uid DESC, c.staged_at DESC" in function
    assert "ORDER BY cf.vendor_fixture_uid, cf.vendor_player_uid, cf.staged_at DESC" in function
    assert "ORDER BY f.fixture_id, p.player_id\n    ON CONFLICT (fixture_id, player_id)" in function


def test_staging_ddl_matches_staging_columns() -> None:
    columns = _table_columns(SCHEMA, "staging_player_feed")
    assert columns == ["batch_id", "staged_at", *STAGING_COLUMNS[1:]]
    assert len(columns) == 42
    assert "CREATE UNLOGGED TABLE IF NOT EXISTS prop_alpha.staging_player_feed" in SCHEMA


def test_core_stats_ddl_and_merge_lists_match_stats_columns() -> None:
    # match_position (migration 007) is not a transform statistic: the merge copies the staged player_position.
    assert _table_columns(SCHEMA, "player_match_stats") == [*STATS_COLUMNS, "source_updated_at", "match_position"]
    function = _merge_function(SCHEMA)
    assert _merge_stats_insert_columns(function) == [*STATS_COLUMNS, "match_position"]
    update_set = function[function.index("ON CONFLICT (fixture_id, player_id) DO UPDATE SET"):]
    for column in [*STATS_COLUMNS[2:], "match_position"]:
        assert re.search(rf"\b{column}\s+= EXCLUDED\.{column}\b", update_set), column


def test_schema_declares_postgres_15_and_named_odds_constraints() -> None:
    assert "CONSTRAINT uq_odds_snapshot UNIQUE NULLS NOT DISTINCT" in SCHEMA
    for constraint in ODDS_CONSTRAINTS + ("chk_some_price",):
        assert f"CONSTRAINT {constraint}" in SCHEMA
    assert "idx_odds_fixture_captured" in SCHEMA and "idx_odds_captured_brin" not in SCHEMA
    assert ") WITH (fillfactor = 90);" in SCHEMA
    assert "ALTER TABLE player_match_stats SET (fillfactor = 90);" in SCHEMA


def test_application_sql_references_existing_columns() -> None:
    raw = set(_table_columns(SCHEMA, "raw_payloads"))
    assert {"source", "vendor_fixture_uid", "payload", "payload_sha256"} <= raw
    lineup = set(_table_columns(SCHEMA, "lineup_expectations"))
    insert = re.search(r"lineup_expectations AS le\s*\((.*?)\)", pipeline.LINEUP_UPSERT_SQL, re.S)
    assert insert and {c.strip() for c in insert.group(1).split(",")} <= lineup
    referees = set(_table_columns(SCHEMA, "referees"))
    updated = set(re.findall(r"(\w+)\s+= v\.", referee_job.UPDATE_SQL))
    assert updated == {
        "referee_id", "league_id", "foul_strictness_coefficient", "yellow_card_propensity", "red_card_propensity",
        "home_bias_ratio",
    }
    assert updated <= referees
    stats = set(_table_columns(SCHEMA, "player_match_stats"))
    assert {"fouls_committed", "yellow_cards", "red_cards", "is_home"} <= stats
    assert {"status", "referee_id", "league_id", "kickoff_at"} <= set(_table_columns(SCHEMA, "fixtures"))


PLAYER_ATTRIBUTES_FROM_LATEST_KICKOFF = (
    "c.player_name, c.player_position, t.team_id, c.vendor_player_uid, c.kickoff_at",
    "ORDER BY c.vendor_player_uid, c.kickoff_at DESC, c.vendor_fixture_uid DESC, c.staged_at DESC",
    "attributes_as_of = EXCLUDED.attributes_as_of,",
    "WHERE pl.attributes_as_of IS NULL OR EXCLUDED.attributes_as_of >= pl.attributes_as_of;",
)


@pytest.mark.parametrize("source", ["schema.sql", "006"])
def test_player_attributes_follow_the_latest_match_not_the_last_row_staged(source: str) -> None:
    """Gate 1 idempotency (2026-10-04): name and position followed the last row staged, so re-ingesting an old
    match rewrote them and concurrent workers made the winner arbitrary."""
    function = _merge_function(MERGE_SOURCES[source])
    for fragment in PLAYER_ATTRIBUTES_FROM_LATEST_KICKOFF:
        assert fragment in function, f"missing player-attribute fragment {fragment!r}"
    assert "ORDER BY c.vendor_player_uid, c.staged_at DESC" not in function
    assert "attributes_as_of  TIMESTAMPTZ" in SCHEMA


def test_migration_006_adds_the_column_replaces_the_merge_and_repairs_in_one_transaction() -> None:
    body = _strip_comments(MIGRATION_006)
    statements = [line.strip() for line in body.splitlines() if line.strip()]
    assert statements[:4] == [
        "SET lock_timeout = '5s';", "BEGIN;",
        "ALTER TABLE prop_alpha.players ADD COLUMN IF NOT EXISTS attributes_as_of TIMESTAMPTZ;",
        "DROP FUNCTION IF EXISTS prop_alpha.merge_staging_player_feed();",
    ]
    assert statements[-2:] == ["COMMIT;", "RESET lock_timeout;"]
    repair = body[body.index("$$;"):]
    assert repair.index("pg_advisory_xact_lock") < repair.index("UPDATE prop_alpha.players AS pl")
    assert "jsonb_to_recordset(r.payload->'players')" in repair
    assert "ORDER BY e.uid, f.kickoff_at DESC, f.vendor_fixture_uid DESC" in repair
    assert "IS DISTINCT FROM (latest.name, latest.position, latest.kickoff_at)" in repair
    for statement in ("TRUNCATE", "VACUUM FULL", "CLUSTER", "ALTER COLUMN", "SET DEFAULT"):
        assert statement not in body


def test_merge_stores_each_matchs_own_position() -> None:
    """Migration 007 (audit 2026-10-04): pricing reads positions as of kickoff from player_match_stats."""
    function = _merge_function(MIGRATION_007)
    assert "match_position           = EXCLUDED.match_position" in function
    assert "x.player_position" in function
    assert "match_position           VARCHAR(16)" in SCHEMA
    assert "ADD COLUMN IF NOT EXISTS match_position" in MIGRATION_007 and "SET lock_timeout" in MIGRATION_007


MIGRATION_008 = (ROOT / "migrations" / "008_incomplete_team_matches.sql").read_text(encoding="utf-8")


def _function(sql: str, name: str) -> str:
    start = sql.index(f"CREATE OR REPLACE FUNCTION prop_alpha.{name}(")
    return sql[start:sql.index("$$;", start) + 3]


def test_incomplete_team_matches_is_mirrored_in_schema_and_read_by_pricing() -> None:
    """Migration 008 (maintainer decision 2026-10-05): club-matches with an unidentified player who played are left
    out of the scan's lineup estimates and league levels."""
    assert _function(SCHEMA, "refresh_incomplete_team_matches") == _function(MIGRATION_008,
                                                                            "refresh_incomplete_team_matches")
    assert "CREATE TABLE IF NOT EXISTS prop_alpha.incomplete_team_matches" in SCHEMA
    assert "SET lock_timeout" in MIGRATION_008 and "TRUNCATE" not in MIGRATION_008
    from prop_alpha.alerts import scan
    for sql in (scan.TEAM_RECENT_SQL, scan.LEVEL_SQL):
        assert "prop_alpha.incomplete_team_matches" in sql and "NOT EXISTS" in sql
