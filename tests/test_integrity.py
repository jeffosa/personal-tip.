from __future__ import annotations

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

    from prop_alpha.calibration import referee_job

    connection = _RecordingConnection()
    monkeypatch.setattr(psycopg2, "connect", lambda dsn: connection)
    assert referee_job.run_referee_job("postgresql://unused", league_id=7) == 2
    statements = [entry for entry in connection.log if entry[0] != "row"]
    assert statements[0] == (referee_job.ADVISORY_LOCK_SQL, (referee_job.MERGE_LOCK_KEY,))
    assert "player_match_stats" in statements[1][0] and statements[1][1] == (7, 730)
    assert "UPDATE prop_alpha.referees AS r" in statements[2][0]
    updated_ids = [args[0] for kind, args in connection.log if kind == "row"]
    assert updated_ids == [1, 2]
    assert connection.closed
