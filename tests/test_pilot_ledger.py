"""The pilot paper ledger and closing-price capture (alerts/pilot_ledger.py, alerts/pilot_closing.py): every pilot tip is
a paper bet that can be settled, reconciled against a bookmaker result and compared with its closing price."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest

from prop_alpha.alerts import ledger, pilot_closing, pilot_ledger
from prop_alpha.alerts.ledger import PlacedBetStore
from prop_alpha.alerts.telegram import AlertContext, demo_case, tip_layout
from prop_alpha.ingestion import oddspapi
from prop_alpha.scanner.slate import UpcomingFixture

NOW = datetime(2026, 10, 11, 18, 50, tzinfo=timezone.utc)
KICKOFF = datetime(2026, 10, 11, 19, 0, tzinfo=timezone.utc)
FIXTURE = UpcomingFixture(500, "Brazil Serie A", "76", "348", "Flamengo", "Fluminense", KICKOFF)
CONTEXT = AlertContext("Flamengo v Fluminense, Sun 11 Oct 19:00 UTC", "Leo Pereira (Flamengo)")
NAMES = ("Alpha Lima", "Bravo Mike", "Charlie Nova")


def _tips(n: int = 2) -> list[Any]:
    """TipLines for n over bets on fixture 500, as the real notifier builds them."""
    _, report = demo_case(NOW)
    base = next(d for d in report.directives if d.line_value == 4.5)
    directives = [replace(base, fixture_id=500, player_id=70 + i, side="over", line_value=1.5, decimal_odds=3.0 + i,
                          sportsbook="bet365") for i in range(n)]
    layout = tip_layout([(d, None, AlertContext(CONTEXT.match, f"{NAMES[i]} (Flamengo)")) for i, d in enumerate(directives)])
    return [tip for _, tips in layout for tip in tips]


def _record(tmp_path: Path, n: int = 2) -> tuple[PlacedBetStore, Path]:
    store, tips_file = PlacedBetStore(tmp_path / "bets.jsonl"), tmp_path / "tips.jsonl"
    pilot_ledger.record_tips(store, tips_file, _tips(n), {500: FIXTURE}, NOW)
    return store, tips_file


# ---------------- recording ----------------
def test_a_tip_priced_void_if_the_player_does_not_start_is_never_recorded(tmp_path: Path) -> None:
    """2026-10-27: the pilot prices bet365 and Betway void if the player does not start, but paper bets are settled void only
    for a player who did not play (replies.grade reads no starter flag), so a sub appearance would be graded, not voided. Such
    a tip raises before anything is written, and so before its message is sent (TelegramNotifier's on_tips)."""
    store, tips_file = PlacedBetStore(tmp_path / "bets.jsonl"), tmp_path / "tips.jsonl"
    tips = [replace(tip, directive=replace(tip.directive, void_rule="void_if_not_starting")) for tip in _tips(2)]
    with pytest.raises(ValueError, match="void_if_not_starting cannot be settled"):
        pilot_ledger.record_tips(store, tips_file, tips, {500: FIXTURE}, NOW)
    assert not tips_file.exists() and store.load_all()[0] == {}
    assert pilot_ledger.SETTLED_VOID_RULE == "void_if_no_appearance"


def test_each_tip_becomes_a_numbered_paper_bet_with_its_odds_and_a_tip_line(tmp_path: Path) -> None:
    store, tips_file = _record(tmp_path)
    bets = store.open_bets(NOW)
    assert [b.bet_id for b in bets] == [1, 2] and [b.odds for b in bets] == [3.0, 4.0]
    assert all(b.side == "over" and b.line == 1.5 and b.book == "bet365" and b.fixture_id == 500 for b in bets)
    assert bets[0].player == "Alpha Lima (Flamengo)" and bets[0].market == "tackles"
    tips = pilot_ledger.load_tips(tips_file)
    assert set(tips) == {1, 2} and tips[1]["league"] == "Brazil Serie A" and tips[1]["kickoff_at"] == KICKOFF.isoformat()
    assert pilot_ledger.fixture_of(tips[1]) == FIXTURE
    # Ids carry on from the file, so a second run never reuses one; only the new selection (the third player) is added.
    [third] = pilot_ledger.record_tips(store, tips_file, _tips(3), {500: FIXTURE}, NOW + timedelta(hours=4))
    assert third.player_id == 72 and [b.bet_id for b in store.open_bets(NOW)] == [1, 2, 3]


def test_a_tip_with_no_fixture_is_refused_and_a_damaged_tip_line_raises(tmp_path: Path) -> None:
    store, tips_file = PlacedBetStore(tmp_path / "b.jsonl"), tmp_path / "t.jsonl"
    with pytest.raises(ValueError, match="no fixture to settle against"):
        pilot_ledger.record_tips(store, tips_file, _tips(1), {}, NOW)
    assert store.open_bets(NOW) == []  # nothing is recorded for a tip that could never be settled
    tips_file.write_text('{"bet_id": 1}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="not a pilot tip record"):
        pilot_ledger.load_tips(tips_file)


# ---------------- settlement and reconciliation ----------------
def _rows(count: int, minutes: int = 90, status: str = "final") -> Mapping[tuple[int, int], Any]:
    return {(500, 70): (status, minutes, count, 0), (500, 71): (status, minutes, count + 5, 0)}


def test_paper_bets_settle_from_the_stored_counts_and_report_a_provisional_total(tmp_path: Path) -> None:
    store, _ = _record(tmp_path)
    # An unfinished match stays open; a player who did not play is void, never a loss.
    assert ledger.settle_open_bets(store.open_bets(NOW), _rows(0, status="scheduled"), NOW) == []
    assert [s.result for _, s in ledger.settle_open_bets(store.open_bets(NOW), _rows(0, minutes=0), NOW)] == ["void", "void"]
    settled = ledger.settle_open_bets(store.open_bets(NOW), _rows(count=2), NOW)  # 2 tackles: over 1.5 wins; the other has 7
    assert [s.result for _, s in settled] == ["won", "won"]
    for _, settlement in settled:
        store.record_settlement(settlement)
    assert store.open_bets(NOW) == []
    text = pilot_ledger.results_text(store.settled_bets(), [])
    assert "2 settled paper bet(s) (2 won, 0 lost, 0 void)" in text and "2 provisional" in text
    assert "0 with a bookmaker result you recorded" in text and "no closing price captured yet" in text


def test_a_recorded_bookmaker_result_is_reconciled_against_the_automatic_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store, _ = _record(tmp_path)
    monkeypatch.setattr(pilot_ledger, "PILOT_BETS_FILE", store.path)
    monkeypatch.setattr(pilot_ledger, "PILOT_CLOSING_FILE", tmp_path / "closing.jsonl")
    monkeypatch.setattr(ledger, "read_settlement_rows", lambda dsn, bets, sql=ledger.SETTLEMENT_SQL: dict(_rows(count=2)))
    assert pilot_ledger.main(["--record", "1", "won", "--book", "bet365"]) == 0
    assert pilot_ledger.main(["--record", "2", "lost", "--book", "bet365"]) == 0
    capsys.readouterr()
    items = ledger.reconcile_settlements(store, "postgresql://pilot", NOW)
    outcomes = {bet.bet_id: item.outcome for bet, item in items}
    # Bet 1: bookmaker WON, automatic WON (2 tackles v 1.5): agree. Bet 2: bookmaker LOST, automatic WON (7 v 1.5): the
    # assumed contract disagrees with the book.
    assert outcomes == {1: "agree", 2: "discrepancy"}
    assert pilot_ledger.main(["--reconcile"]) == 1
    report = capsys.readouterr().out
    assert "DISCREPANCY" in report and "AGREE" in report and "PRD Gate 3 needs every bet to reconcile" in report


def test_the_record_command_refuses_an_unknown_bet_and_a_bad_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = _record(tmp_path)
    monkeypatch.setattr(pilot_ledger, "PILOT_BETS_FILE", store.path)
    with pytest.raises(SystemExit):
        pilot_ledger.main(["--record", "9", "won"])
    with pytest.raises(SystemExit):
        pilot_ledger.main(["--record", "1", "maybe"])


def test_reconstructed_paper_bets_are_kept_out_of_the_model_scored_total(tmp_path: Path) -> None:
    store, tips_file = _record(tmp_path)
    for _, settlement in ledger.settle_open_bets(store.open_bets(NOW), _rows(count=2), NOW):
        store.record_settlement(settlement)
    records = pilot_ledger.load_tips(tips_file)
    assert pilot_ledger.reconstructed_ids(records) == set()
    records[2]["reconstructed"] = True
    rebuilt = pilot_ledger.reconstructed_ids(records)
    text = pilot_ledger.results_text(store.settled_bets(), [], rebuilt)
    assert rebuilt == {2} and "1 reconstructed (no model numbers, excluded from model scoring): model-scored 1 settled" in text
    assert "reconstructed" not in pilot_ledger.results_text(store.settled_bets(), [])


def _settle_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sent: list[str], fail: bool = False,
                  queries: list[str] | None = None) -> PlacedBetStore:
    store, _ = _record(tmp_path)
    monkeypatch.setattr(pilot_ledger, "PILOT_BETS_FILE", store.path)
    monkeypatch.setattr(pilot_ledger, "PILOT_CLOSING_FILE", tmp_path / "closing.jsonl")
    monkeypatch.setattr(ledger, "DEFAULT_LOCK_FILE", tmp_path / "ledger.lock")  # never the real ledger's lock or queue
    monkeypatch.setattr(ledger, "DEFAULT_ANSWERS_FILE", tmp_path / "answers.jsonl")
    def rows(dsn: str, bets: object, settlement_sql: str = ledger.SETTLEMENT_SQL) -> dict[Any, Any]:
        if queries is not None:
            queries.append(settlement_sql)
        return dict(_rows(count=0))

    monkeypatch.setattr(ledger, "read_settlement_rows", rows)

    class _Notifier:
        def __init__(self, config: object) -> None:
            self.config = config

        def send_message(self, text: str) -> int:
            if fail:
                raise ConnectionError("Telegram unreachable")
            sent.append(text)
            return 1

    monkeypatch.setattr(pilot_ledger, "TelegramNotifier", _Notifier)
    monkeypatch.setattr(pilot_ledger, "load_config", lambda: object())
    return store


def test_paper_bets_settle_on_the_pilot_count_and_the_real_ledger_keeps_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pilot's paper bets settle on the count it prices (pilot_ledger.PILOT_TACKLES_SQL: plain tackles_attempted again since
    2026-10-13), through its own settlement query; the League One/Two ledger's query is unchanged."""
    queries: list[str] = []
    _settle_setup(tmp_path, monkeypatch, [], queries=queries)
    assert pilot_ledger.main(["--settle"]) == 0
    assert queries == [pilot_ledger.PILOT_SETTLEMENT_SQL]
    assert pilot_ledger.PILOT_SETTLEMENT_SQL == ledger.SETTLEMENT_SQL  # the same count as League One/Two again
    wider = pilot_ledger.recount(ledger.SETTLEMENT_SQL, pilot_ledger.TACKLES_PLUS_CHALLENGES_LOST_SQL)
    assert "(s.tackles_attempted + s.dribbled_past), s.fouls_committed" in wider
    assert "s.minutes_played, s.tackles_attempted, s.fouls_committed" in ledger.SETTLEMENT_SQL
    for bad in ("SELECT 1", "SELECT s.tackles_attempted, s.tackles_attempted"):
        with pytest.raises(ValueError, match="exactly one s.tackles_attempted"):
            pilot_ledger.recount(bad)


def test_settle_sends_the_new_results_only_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sent: list[str] = []
    _settle_setup(tmp_path, monkeypatch, sent)
    locks: list[Path] = []
    original_lock = ledger.ledger_lock

    def record_lock(path: Path = ledger.DEFAULT_LOCK_FILE) -> Any:
        locks.append(path)
        return original_lock(path)

    monkeypatch.setattr(ledger, "ledger_lock", record_lock)
    assert pilot_ledger.main(["--settle", "--send"]) == 0
    assert locks == [ledger.DEFAULT_LOCK_FILE, pilot_ledger.lock_for(PlacedBetStore(pilot_ledger.PILOT_BETS_FILE))]
    assert "#1 LOST (provisional)" in capsys.readouterr().out
    assert len(sent) == 1 and sent[0].startswith(pilot_ledger.SETTLED_HEADER) and "never bets you placed" in sent[0]
    assert ledger.AnswerOutbox(tmp_path / "answers.jsonl").unsent() == []  # queued, then marked sent
    assert pilot_ledger.main(["--settle", "--send"]) == 0 and len(sent) == 1  # nothing new: nothing sent


def test_a_settlement_notice_that_fails_to_send_stays_queued_for_the_next_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Audit 2026-10-11 (second pass): the settlements were recorded before the send, so a failed send was lost for good."""
    store = _settle_setup(tmp_path, monkeypatch, [], fail=True)
    with pytest.raises(ConnectionError):
        pilot_ledger.main(["--settle", "--send"])
    capsys.readouterr()
    assert len(store.settled_bets()) == 2  # the results are kept
    [(answer_id, reply_to, text, attempts)] = ledger.AnswerOutbox(tmp_path / "answers.jsonl").unsent()
    assert reply_to is None and text.startswith(pilot_ledger.SETTLED_HEADER) and attempts == 1  # the poll re-sends it


def test_recording_is_idempotent_locked_and_never_leaves_a_bet_without_its_tip_line(tmp_path: Path) -> None:
    store, tips_file = _record(tmp_path)
    assert pilot_ledger.record_tips(store, tips_file, _tips(), {500: FIXTURE}, NOW) == []  # an overlapping run adds nothing
    assert [b.bet_id for b in store.open_bets(NOW)] == [1, 2]
    with ledger.ledger_lock(pilot_ledger.lock_for(store)):  # another writer holds the store
        with pytest.raises(OSError):
            pilot_ledger.record_tips(store, tips_file, _tips(3), {500: FIXTURE}, NOW)
    assert [b.bet_id for b in store.open_bets(NOW)] == [1, 2]
    # A crash after the tip line and before the bet: the tip line is an orphan nothing reads, and a later run records the bet.
    crash_store, crash_tips = PlacedBetStore(tmp_path / "c.jsonl"), tmp_path / "c_tips.jsonl"

    def crash(bet: Any) -> None:
        raise OSError("disk full")

    original = crash_store.record_bet
    crash_store.record_bet = crash  # type: ignore[method-assign]
    with pytest.raises(OSError):
        pilot_ledger.record_tips(crash_store, crash_tips, _tips(1), {500: FIXTURE}, NOW)
    assert crash_store.open_bets(NOW) == [] and len(pilot_ledger.load_tips(crash_tips)) == 1
    crash_store.record_bet = original  # type: ignore[method-assign]
    [bet] = pilot_ledger.record_tips(crash_store, crash_tips, _tips(1), {500: FIXTURE}, NOW)
    assert bet.bet_id == 1 and set(pilot_ledger.load_tips(crash_tips)) == {1}


# ---------------- closing prices ----------------
def test_closing_price_is_the_tipped_books_latest_and_gives_a_price_based_value(tmp_path: Path) -> None:
    store, _ = _record(tmp_path, n=1)
    [bet] = store.open_bets(NOW)

    def quote(book: str, over: float, at: str, player: int = 70) -> dict[str, Any]:
        return {"captured_at": at, "fixture_id": 500, "player_id": player, "market": "tackles", "line": 1.5, "book": book,
                "over": over, "under": None}

    closing = [quote("betway", 2.0, "2026-10-11T18:55:00+00:00"), quote("bet365", 2.5, "2026-10-11T18:50:00+00:00"),
               quote("bet365", 2.4, "2026-10-11T18:56:00+00:00"), quote("bet365", 9.0, "2026-10-11T18:57:00+00:00", player=99)]
    assert pilot_ledger.closing_price(bet, closing) == (2.4, "bet365")  # the tipped book, its latest capture
    assert pilot_ledger.closing_price(bet, [quote("bet365", 2.4, "x", player=99)]) is None
    # Only the tipped book's close: Betway's has another margin (PRD Gate 3, 2026-10-25).
    assert pilot_ledger.closing_price(bet, [quote("betway", 2.0, "2026-10-11T18:55:00+00:00")]) is None
    assert pilot_ledger.price_clv(3.0, 2.4) == pytest.approx(0.25)  # took 3.00, the market closed at 2.40
    assert "mean closing-price value +25.0% over 1 paper bet(s)" in pilot_ledger.clv_summary([bet], closing)
    assert pilot_ledger.clv_summary([bet], []) == "no closing price captured yet"
    with pytest.raises(ValueError, match="above 1"):
        pilot_ledger.price_clv(1.0, 2.0)


def test_closing_capture_is_due_only_for_open_bets_kicking_off_within_fifteen_minutes(tmp_path: Path) -> None:
    store, tips_file = _record(tmp_path)
    tips, open_bets = pilot_ledger.load_tips(tips_file), store.open_bets(NOW)
    assert [f.fixture_id for f in pilot_closing.due_fixtures(open_bets, tips, set(), NOW)] == [500]
    assert pilot_closing.due_fixtures(open_bets, tips, {500}, NOW) == []  # handled once
    assert pilot_closing.due_fixtures(open_bets, tips, set(), KICKOFF - timedelta(minutes=20)) == []  # too early
    assert pilot_closing.due_fixtures(open_bets, tips, set(), KICKOFF) == []  # already kicked off
    assert pilot_closing.due_fixtures([], tips, set(), NOW) == []  # no open bet
    with pytest.raises(ValueError, match="UTC"):
        pilot_closing.due_fixtures(open_bets, tips, set(), datetime(2026, 10, 11, 18, 50))
    squads = pilot_closing.squads_of(open_bets)
    assert [p.name for p in squads[500]] == ["Alpha Lima", "Bravo Mike"]  # the club in brackets is not part of the name


def _ladder_reply(price_by_player: Mapping[str, float]) -> list[dict[str, Any]]:
    """A bet365 reply with the tackles ladder: the "2+" step (Over 1.5) for each named player."""
    players = {f"p{i}": {"active": True, "price": price, "playerName": name, "limit": 100}
               for i, (name, price) in enumerate(price_by_player.items())}
    return [{"fixtureId": "id500", "participant1Id": 11, "participant2Id": 12, "startTime": KICKOFF.isoformat(),
             "bookmakerOdds": {"bet365": {"bookmakerIsActive": True, "markets": {
                 "5001": {"marketActive": True, "outcomes": {"2": {"players": players}}}}}}}]


class _FakeOdds:
    def __init__(self, body: Any, left: int = 200) -> None:
        self.body, self.left, self.requests_sent = body, left, 0
        self.paths: list[str] = []

    def get_json_value(self, path: str, empty_on: Mapping[int, str] | None = None) -> Any:
        self.paths.append(path)
        if path == oddspapi.ACCOUNT_PATH:
            return {"current_subscription_id": "s",
                    "subscriptions": [{"subscription_id": "s", "request_limit": 250, "request_count": 250 - self.left}]}
        self.requests_sent += 1
        if path == oddspapi.MARKETS_PATH:
            return [{"marketId": 5001, "marketType": "players-tackles", "playerProp": True,
                     "outcomes": [{"outcomeId": 2, "outcomeName": "2+"}]}]
        return self.body


def _names(tmp_path: Path) -> None:
    cache = tmp_path / "oddspapi"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "participants.json").write_text(json.dumps({"11": "Flamengo", "12": "Fluminense"}), encoding="utf-8")


def test_capture_writes_the_tipped_selections_closing_prices_once_per_fixture(tmp_path: Path) -> None:
    store, tips_file = _record(tmp_path)
    _names(tmp_path)
    paths: dict[str, Any] = {"closing_file": tmp_path / "closing.jsonl", "handled_file": tmp_path / "handled.json", "tips_file": tips_file}
    odds = _FakeOdds(_ladder_reply({"Alpha Lima": 2.4, "Bravo Mike": 3.5, "Somebody Else": 1.9}))
    written, notes, available = pilot_closing.capture(NOW, odds, store, tmp_path, **paths)
    assert available and written == 2 and any("closing prices: 2 for 1 fixture(s)" in n for n in notes)
    closing = pilot_ledger.load_closing(paths["closing_file"])
    assert sorted((q["player_id"], q["book"], q["over"]) for q in closing) == [(70, "bet365", 2.4), (71, "bet365", 3.5)]
    first, second = store.open_bets(NOW)
    assert pilot_ledger.closing_price(first, closing) == (2.4, "bet365")
    assert pilot_ledger.price_clv(first.odds, 2.4) == pytest.approx(3.0 / 2.4 - 1.0)
    sent_before = odds.requests_sent
    assert pilot_closing.capture(NOW, odds, store, tmp_path, **paths) == (0, [], True)  # handled: no second request
    assert odds.requests_sent == sent_before and second.bet_id == 2


def test_capture_leaves_a_fixture_for_the_next_poll_when_odds_could_not_be_fetched(tmp_path: Path) -> None:
    store, tips_file = _record(tmp_path)
    paths: dict[str, Any] = {"closing_file": tmp_path / "closing.jsonl", "handled_file": tmp_path / "handled.json", "tips_file": tips_file}
    odds = _FakeOdds([], left=20)  # at the reserve: nothing is fetched
    written, notes, available = pilot_closing.capture(NOW, odds, store, tmp_path, **paths)
    assert (written, available) == (0, False) and "Odds NOT fetched" in notes[0]
    assert not paths["handled_file"].exists() and odds.requests_sent == 0


def test_no_request_and_no_client_when_nothing_is_due(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pilot_ledger, "PILOT_BETS_FILE", tmp_path / "none.jsonl")
    monkeypatch.setattr(pilot_ledger, "PILOT_TIPS_FILE", tmp_path / "none_tips.jsonl")
    monkeypatch.setattr(pilot_closing, "HANDLED_FILE", tmp_path / "h.json")
    monkeypatch.setattr(pilot_closing, "PILOT_SHADOW_FILE", tmp_path / "shadow.jsonl")
    import prop_alpha.ingestion.pipeline as pipeline

    monkeypatch.setattr(pipeline, "build_odds_client", lambda env: pytest.fail("no client is built when nothing is due"))
    assert pilot_closing.main([]) == 0


def test_the_new_modules_add_no_exception_handler() -> None:
    import ast

    for module in (pilot_ledger, pilot_closing):
        assert module.__file__ is not None
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        assert not [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)], module.__name__


def test_record_names_the_bookmaker_the_result_was_read_from(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                             capsys: pytest.CaptureFixture[str]) -> None:
    # 2026-10-25: results read off another bookmaker's slip (SportyBet tickets for bet365 tips) are recorded with that book.
    store, _ = _record(tmp_path, 1)
    monkeypatch.setattr(pilot_ledger, "PILOT_BETS_FILE", store.path)
    assert pilot_ledger.main(["--record", "1", "lost", "--book", "sportybet"]) == 0
    [(_, settlement)] = store.settled_bets()
    assert (settlement.result, settlement.source) == ("lost", "manual") and "(sportybet)" in settlement.detail
    assert settlement.book == "sportybet"  # stored as a field, so reconcile can tell it from bet365's own result
    assert "(sportybet)" in capsys.readouterr().out


def test_a_recorded_result_naming_no_bookmaker_never_reconciles_as_the_tipped_books(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """M2 (audit 2026-10-26): `--record 1 lost` with no --book (e.g. read off a SportyBet ticket) is unverified."""
    store, _ = _record(tmp_path)
    monkeypatch.setattr(pilot_ledger, "PILOT_BETS_FILE", store.path)
    monkeypatch.setattr(pilot_ledger, "PILOT_CLOSING_FILE", tmp_path / "closing.jsonl")
    monkeypatch.setattr(ledger, "read_settlement_rows", lambda dsn, bets, sql=ledger.SETTLEMENT_SQL: dict(_rows(count=2)))
    assert pilot_ledger.main(["--record", "1", "won"]) == 0
    capsys.readouterr()
    [(_, item)] = [(b, i) for b, i in ledger.reconcile_settlements(store, "postgresql://pilot", NOW) if b.bet_id == 1]
    assert item.outcome == "unnamed_book_agree"
    assert pilot_ledger.main(["--reconcile"]) == 0
    assert "NO BOOK NAMED, AGREES" in capsys.readouterr().out
    assert pilot_ledger.main(["--list"]) == 0
    assert "(1 naming no bookmaker: unverified)" in capsys.readouterr().out
