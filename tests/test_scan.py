from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import requests

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
from prop_alpha.alerts.telegram import DEFAULT_CRITERIA, PAPER_LABEL
from prop_alpha.modeling.engine import VoidRule
from prop_alpha.scanner.market_scanner import expected_value, stressed_probabilities
from prop_alpha.scanner.slate import (
    FAIR_LINES,
    FAIR_MIN_ODDS_RANGE,
    MOCK_SPORTSBOOK,
    SCAN_GAME_STATE_K,
    Appearance,
    FairPrice,
    SquadPlayer,
    UpcomingFixture,
    build_projections,
    fair_price_board,
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


class _StubSportmonks:
    def get_json(self, path: str) -> dict[str, Any]:
        row = {"id": 9001, "league_id": 14, "state": {"developer_name": "NS"},
               "starting_at_timestamp": int(KICKOFF.timestamp()),
               "participants": [{"id": 76, "name": "Walsall", "meta": {"location": "home"}},
                                {"id": 348, "name": "Port Vale", "meta": {"location": "away"}}]}
        return {"data": [row], "pagination": {"has_more": False}}


class _FailingOdds:
    requests_sent = 0

    def get_json_value(self, path: str, empty_on: frozenset[int] = frozenset()) -> Any:
        raise requests.HTTPError(f"{path}: HTTP 403: Forbidden")


def _patch_database(monkeypatch: pytest.MonkeyPatch) -> None:
    squad, history, recent = _inputs()
    inputs = scan.SlateInputs(tuple(squad), {k: tuple(v) for k, v in history.items()},
                              {k: tuple(v) for k, v in recent.items()}, RATES)

    class _Conn:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def cursor(self) -> Any:
            return self

        def close(self) -> None:
            return None

    import psycopg2

    monkeypatch.setattr(psycopg2, "connect", lambda *args, **kwargs: _Conn())
    monkeypatch.setattr(scan, "load_slate_inputs", lambda cur, fixture: inputs)


def test_odds_outage_is_reported_in_the_summary_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Audit rerun 2026-10-03: an OddsPapi 403 used to end the run before any summary was sent. With no odds,
    the match gets a fair-price board instead of high-value alerts."""
    _patch_database(monkeypatch)
    store = SentAlertStore(tmp_path / "sent.jsonl")
    notifier = _RecordingNotifier(store)
    with caplog.at_level(logging.ERROR, logger="prop_alpha.scan"):
        summary = scan.run_scan(KICKOFF - timedelta(hours=2), _StubSportmonks(), "postgresql://unused", notifier,
                                store, 1000.0, "", odds="oddspapi", odds_client=_FailingOdds(), state_dir=tmp_path)
    assert summary.fixtures == ["Walsall v Port Vale 14:00"]
    assert summary.quotes == 0 and summary.alerts_sent == 0
    assert summary.boards_sent == 1 and notifier.texts and all(t.startswith(PAPER_LABEL) for t in notifier.texts)
    text = summary.text("")
    assert "Odds unavailable this run, nothing priced: HTTPError" in text and "HTTP 403" in text
    assert "Matches kicking off in this window: 1" in text
    assert "Fair-price boards sent (matches with no bookmaker line): 1" in text


# ---------------- fair-price board ----------------
def _projections(rates: dict[tuple[str, str], float] = RATES) -> dict[Any, Any]:
    squad, history, recent = _inputs()
    projections, _ = build_projections(FIXTURE, squad, history, recent, rates, QuantEngine(game_state_k=6.0))
    return projections


def test_board_minimum_odds_give_the_screens_profit_after_the_loss_floor() -> None:
    floor, min_ev = DEFAULT_CRITERIA.loss_floor, DEFAULT_CRITERIA.min_ev_per_unit
    projections = _projections()
    board = fair_price_board(projections, floor, min_ev)
    assert board
    low, high = FAIR_MIN_ODDS_RANGE
    for entry in board:
        assert low <= entry.min_odds <= high and entry.fair_odds < entry.min_odds
        stressed = stressed_probabilities(
            projections[(entry.fixture_id, entry.player_id, entry.market)].side_probabilities(
                entry.line, entry.side, VoidRule.VOID_IF_NO_APPEARANCE), floor)
        assert expected_value(stressed, entry.min_odds) >= min_ev - 1e-9
        assert expected_value(stressed, entry.min_odds - 0.01) < min_ev  # rounded up by less than one tick


def test_board_lists_the_line_closest_to_even_for_each_player_and_market() -> None:
    projections = _projections()
    for entry in fair_price_board(projections, DEFAULT_CRITERIA.loss_floor, DEFAULT_CRITERIA.min_ev_per_unit):
        projection = projections[(entry.fixture_id, entry.player_id, entry.market)]

        def gap(line: float) -> float:
            over = projection.side_probabilities(line, "over", VoidRule.VOID_IF_NO_APPEARANCE)
            return abs(over.p_win / (over.p_win + over.p_lose) - 0.5)

        assert gap(entry.line) == min(gap(line) for line in FAIR_LINES)


def test_board_leaves_out_near_certain_and_long_shot_sides() -> None:
    """A player who almost never fouls: the Under is near-certain (minimum odds about 1.05) and the Over a long
    shot, so neither side is listed, though the model priced both."""
    squad, history, recent = _inputs()
    history[1] = [Appearance(1, a.fixture_id, "76", a.kickoff_at, a.started, a.minutes, a.tackles, 0)
                  for a in history[1]]
    projections, _ = build_projections(FIXTURE, squad, history, recent, {**RATES, ("MID", "fouls"): 0.05},
                                       QuantEngine(game_state_k=6.0))
    assert (9001, 1, "fouls") in projections
    board = fair_price_board(projections, DEFAULT_CRITERIA.loss_floor, DEFAULT_CRITERIA.min_ev_per_unit)
    assert not [e for e in board if (e.player_id, e.market) == (1, "fouls")]


def test_board_inputs_and_entries_are_validated() -> None:
    with pytest.raises(ValueError):
        fair_price_board({}, 1.0, 0.04)
    with pytest.raises(ValueError):
        fair_price_board({}, 0.03, 0.0)
    with pytest.raises(ValueError):
        FairPrice(1, 1, "fouls", 1.5, "both", 0.5, 2.0, 2.1)
    with pytest.raises(ValueError):
        FairPrice(1, 1, "fouls", 1.5, "over", 0.5, 2.0, 1.9)


def test_board_is_sent_once_per_match_and_never_for_mock_or_quoted_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_database(monkeypatch)
    store = SentAlertStore(tmp_path / "sent.jsonl")
    first, again = _RecordingNotifier(store), _RecordingNotifier(store)
    now = KICKOFF - timedelta(hours=2)
    summary = scan.run_scan(now, _StubSportmonks(), "postgresql://unused", first, store, 1000.0, "",
                            odds="oddspapi", odds_client=_FailingOdds(), state_dir=tmp_path)
    assert summary.boards_sent == 1
    board_text = "\n".join(first.texts)
    assert first.texts[0].startswith(PAPER_LABEL) and scan.BOARD_HEADER in first.texts[0]
    assert "Walsall v Port Vale, Sat 10 Oct 14:00 UTC" in board_text
    assert "Regular Starter (Walsall)" in board_text and ">= " in board_text
    assert "Rarely Plays" not in board_text  # not a regular starter, so never priced
    retry = scan.run_scan(now, _StubSportmonks(), "postgresql://unused", again, store, 1000.0, "",
                          odds="oddspapi", odds_client=_FailingOdds(), state_dir=tmp_path)
    assert retry.boards_sent == 0 and not [t for t in again.texts if scan.BOARD_HEADER in t]
    mock_store = SentAlertStore(tmp_path / "mock.jsonl")
    mocked = scan.run_scan(now, _StubSportmonks(), "postgresql://unused", _RecordingNotifier(mock_store), mock_store,
                           1000.0, scan.MOCK_LABEL, odds="mock", state_dir=tmp_path / "mock_state")
    assert mocked.boards_sent == 0


def test_board_messages_split_and_keep_the_paper_label() -> None:
    squad = tuple(SquadPlayer(i, f"Player {i:02d}", "MID", "76") for i in range(1, 40))
    inputs = scan.SlateInputs(squad, {}, {}, RATES)
    entries = [FairPrice(9001, i, "fouls", 1.5, "over", 0.5, 2.0, 2.12) for i in range(1, 40)]
    messages = scan.format_board(FIXTURE, inputs, entries)
    texts = [text for text, _ in messages]
    assert len(texts) == 2 and all(t.startswith(PAPER_LABEL) and len(t) < 4096 for t in texts)
    assert scan.BOARD_HEADER in texts[0] and scan.BOARD_HEADER not in texts[1] and "(continued)" in texts[1]
    assert "1. Player 01 (Walsall) fouls 1.5: Over >= 2.12 (model: over 50%)" in texts[0]
    # Numbers run on across the messages, and each message indexes exactly the rows it shows.
    assert [item.number for item in messages[0][1]] == list(range(1, 26))
    assert [item.number for item in messages[1][1]] == list(range(26, 40))
    assert messages[1][1][0].player == "Player 26 (Walsall)" and messages[1][1][0].sides == {"over": 2.12}
    assert all(item.kind == "board" and item.book is None for _, items in messages for item in items)
