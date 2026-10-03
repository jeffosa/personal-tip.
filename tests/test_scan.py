from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from prop_alpha.alerts import scan
from prop_alpha.alerts.telegram import SentAlertStore, TelegramConfig, TelegramNotifier
from prop_alpha.ingestion import sportmonks
from prop_alpha.modeling.engine import QuantEngine
from prop_alpha.scanner.market_scanner import (
    MODEL_RISK_LOSS_FLOOR,
    AllocationReport,
    ExposureBook,
    InstitutionalAssetManager,
)
from prop_alpha.scanner.slate import (
    MOCK_SPORTSBOOK,
    SCAN_GAME_STATE_K,
    Appearance,
    SquadPlayer,
    UpcomingFixture,
    build_projections,
    lineup_estimate,
    mock_quotes,
    scan_window,
)

FAKE_TOKEN = "123456789" + ":" + "AAH" + "x" * 32
KICKOFF = datetime(2026, 10, 10, 14, tzinfo=timezone.utc)
FIXTURE = UpcomingFixture(9001, "League Two", "76", "348", "Walsall", "Port Vale", KICKOFF)
RATES = {(position, market): rate for position in ("DEF", "MID") for market, rate in (("tackles", 1.8), ("fouls", 1.2))}


def _history(player_id: int, team: str, starts: int, n: int = 10) -> list[Appearance]:
    """n past matches, one per week; the first `starts` of the team's 5 most recent are starts."""
    rows = []
    for week in range(n):
        recent_index = n - 1 - week
        started = recent_index < starts if recent_index < 5 else True
        rows.append(Appearance(player_id, 100 + week, team, KICKOFF - timedelta(days=7 * (n - week)), started,
                               90.0 if started else 0.0, 2 if started else 0, 1 if started else 0))
    return rows


def _inputs() -> tuple[list[SquadPlayer], dict[int, list[Appearance]], dict[str, list[int]]]:
    squad = [SquadPlayer(1, "Regular Starter", "MID", "76"), SquadPlayer(2, "Rarely Plays", "DEF", "76"),
             SquadPlayer(3, "Away Starter", "DEF", "348")]
    history = {1: _history(1, "76", 5), 2: _history(2, "76", 1), 3: _history(3, "348", 4)}
    recent = {"76": [105, 106, 107, 108, 109], "348": [105, 106, 107, 108, 109]}
    return squad, history, recent


def test_scan_window_ends_at_the_next_four_hour_boundary() -> None:
    assert scan_window(datetime(2026, 10, 10, 12, 0, 30, tzinfo=timezone.utc))[1] == datetime(2026, 10, 10, 16, tzinfo=timezone.utc)
    assert scan_window(datetime(2026, 10, 10, 23, 59, tzinfo=timezone.utc))[1] == datetime(2026, 10, 11, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        scan_window(datetime(2026, 10, 10, 12))


def test_lineup_estimate_uses_the_teams_recent_fixtures() -> None:
    lineup = lineup_estimate([105, 106, 107, 108, 109], _history(1, "76", 3))
    assert lineup is not None and lineup.p_start == pytest.approx(0.6) and lineup.exp_minutes_start == 90.0
    assert lineup_estimate([], _history(1, "76", 3)) is None


def test_projections_cover_regular_starters_only_and_use_prior_matches() -> None:
    squad, history, recent = _inputs()
    history[1].append(Appearance(1, 999, "76", KICKOFF + timedelta(days=1), True, 90.0, 40, 40))  # after kickoff
    projections, skipped = build_projections(FIXTURE, squad, history, recent, RATES, QuantEngine(game_state_k=SCAN_GAME_STATE_K))
    assert set(projections) == {(9001, p, m) for p in (1, 3) for m in ("tackles", "fouls")}
    assert skipped == []
    assert projections[(9001, 1, "tackles")].rate_posterior.alpha < 40  # the post-kickoff 40 tackles were ignored


def test_missing_league_rate_is_reported_not_guessed() -> None:
    squad, history, recent = _inputs()
    projections, skipped = build_projections(FIXTURE, squad, history, recent, {}, QuantEngine(game_state_k=6.0))
    assert projections == {} and len(skipped) == 4


def test_mock_quotes_are_labelled_two_way_and_reproducible() -> None:
    squad, history, recent = _inputs()
    projections, _ = build_projections(FIXTURE, squad, history, recent, RATES, QuantEngine(game_state_k=6.0))
    first = mock_quotes(projections, KICKOFF - timedelta(hours=2), np.random.default_rng(7))
    again = mock_quotes(projections, KICKOFF - timedelta(hours=2), np.random.default_rng(7))
    assert first and first == again
    assert {q.sportsbook for q in first} == {MOCK_SPORTSBOOK}
    for quote in first:
        assert quote.over_price is not None and quote.under_price is not None
        assert 1.0 / quote.over_price + 1.0 / quote.under_price == pytest.approx(1.06, abs=0.01)


def test_upcoming_listing_keeps_scan_leagues_and_not_started_fixtures() -> None:
    def fixture(fid: int, league: int, state: str) -> dict[str, Any]:
        return {"id": fid, "league_id": league, "state": {"developer_name": state},
                "starting_at_timestamp": int(KICKOFF.timestamp()),
                "participants": [{"id": 76, "name": "Walsall", "meta": {"location": "home"}},
                                 {"id": 348, "name": "Port Vale", "meta": {"location": "away"}}]}
    body = {"data": [fixture(1, 14, "NS"), fixture(2, 14, "FT"), fixture(3, 8, "NS")], "pagination": {"has_more": False}}
    rows, more = sportmonks.upcoming_fixtures(body)
    assert [r["fixture_id"] for r in rows] == [1] and not more
    assert UpcomingFixture(**rows[0]).name == "Walsall v Port Vale"
    assert "fixtureLeagues:12,14" in sportmonks.upcoming_path(KICKOFF, KICKOFF, 1)


def _mock_source(now: datetime, seed: int) -> scan.QuoteSource:
    rng = np.random.default_rng(seed)
    return lambda fixture, projections: mock_quotes(projections, now, rng)


class _RecordingNotifier(TelegramNotifier):
    def __init__(self, store: SentAlertStore) -> None:
        super().__init__(TelegramConfig(FAKE_TOKEN, "1"), store=store, sleep=lambda s: None)
        self.texts: list[str] = []

    def send_message(self, text: str) -> int:
        self.texts.append(text)
        return len(self.texts)


def _scan_one(tmp_path: Path, max_alerts: int) -> tuple[scan.ScanSummary, _RecordingNotifier]:
    squad, history, recent = _inputs()
    inputs = scan.SlateInputs(tuple(squad), {k: tuple(v) for k, v in history.items()},
                              {k: tuple(v) for k, v in recent.items()}, RATES)
    now = KICKOFF - timedelta(hours=2)
    summary = scan.ScanSummary(*scan_window(now), alert_cap=max_alerts)
    manager = InstitutionalAssetManager(1000.0, model_risk_loss_floor=MODEL_RISK_LOSS_FLOOR)
    projections, report, _ = scan.scan_fixture(FIXTURE, inputs, now, _mock_source(now, 3), manager,
                                               QuantEngine(game_state_k=6.0), summary, ExposureBook({}))
    assert report is not None
    store = SentAlertStore(tmp_path / "mock.jsonl")
    notifier = _RecordingNotifier(store)
    report.directives.sort(key=lambda d: -d.ev_per_unit)
    summary.alerts_sent, _ = notifier.notify_digest(report, projections, label=scan.MOCK_LABEL,
                                                    descriptions=scan.descriptions(FIXTURE, inputs, projections),
                                                    max_alerts=max_alerts)
    return summary, notifier


def test_alerts_carry_the_mock_label_names_and_respect_the_cap(tmp_path: Path) -> None:
    summary, notifier = _scan_one(tmp_path, max_alerts=1)
    assert summary.alerts_sent == len(notifier.texts) == 1
    for text in notifier.texts:
        assert text.startswith(scan.MOCK_LABEL)
        assert "Walsall v Port Vale" in text and ("Regular Starter" in text or "Away Starter" in text)
    assert scan.MOCK_LABEL in summary.text(scan.MOCK_LABEL)


def test_a_fixture_that_cannot_be_priced_is_recorded_and_skipped(caplog: pytest.LogCaptureFixture) -> None:
    squad, history, recent = _inputs()
    # No tackles ever recorded and a near-zero league rate: the engine refuses the degenerate prior.
    history[1] = [Appearance(1, a.fixture_id, "76", a.kickoff_at, a.started, a.minutes, 0, 0) for a in history[1]]
    rates = {**RATES, ("MID", "tackles"): 1e-12}
    inputs = scan.SlateInputs(tuple(squad), {k: tuple(v) for k, v in history.items()},
                              {k: tuple(v) for k, v in recent.items()}, rates)
    summary = scan.ScanSummary(*scan_window(KICKOFF - timedelta(hours=2)))
    manager = InstitutionalAssetManager(1000.0, model_risk_loss_floor=MODEL_RISK_LOSS_FLOOR)
    with caplog.at_level(logging.ERROR, logger="prop_alpha.scan"):
        projections, report, quotes = scan.scan_fixture(
            FIXTURE, inputs, KICKOFF - timedelta(hours=2), _mock_source(KICKOFF - timedelta(hours=2), 1), manager,
            QuantEngine(game_state_k=6.0), summary, ExposureBook({}),
        )
    assert (projections, report, quotes) == ({}, None, [])
    assert summary.failures and "Walsall v Port Vale" in summary.failures[0]
    assert "Could not price Walsall v Port Vale" in summary.text("")


def test_notify_max_alerts_must_be_non_negative(tmp_path: Path) -> None:
    notifier = _RecordingNotifier(SentAlertStore(tmp_path / "s.jsonl"))
    with pytest.raises(ValueError):
        notifier.notify(AllocationReport(), {}, max_alerts=-1)
