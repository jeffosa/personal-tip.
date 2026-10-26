"""Results job (ingestion/results.py): list finished League One/Two matches and ingest the ones not yet stored. The data
vendor is Sportmonks (again since 2026-10-21; TheStatsAPI from 2026-10-15 to 2026-10-20)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

from prop_alpha.ingestion import results, sportmonks
from prop_alpha.ingestion.pipeline import FixtureResult, MergeResult, SlateReport

NOW = datetime(2026, 10, 10, 20, 0, tzinfo=timezone.utc)
LATEST = datetime(2026, 10, 3, 14, 0, tzinfo=timezone.utc)


def _row(number: int, league: int = 14, state: str = "FT", kickoff: datetime = LATEST) -> dict[str, Any]:
    """A Sportmonks fixture listing row (league 14 = League Two)."""
    return {"id": number, "league_id": league, "state": {"developer_name": state},
            "starting_at_timestamp": int(kickoff.timestamp()),
            "participants": [{"id": 76, "name": "Walsall", "meta": {"location": "home"}},
                             {"id": 348, "name": "Port Vale", "meta": {"location": "away"}}]}


def _uid(number: int) -> str:
    return str(number)


def test_finished_listing_keeps_final_scan_league_fixtures() -> None:
    body = {"data": [_row(1), _row(2, state="POSTPONED"), _row(3, league=8), _row(4, 12, "AET", LATEST + timedelta(days=1)),
                     _row(5, 12, "NS", NOW)], "pagination": {"has_more": True}}
    rows, more = sportmonks.finished_fixtures(body)
    assert [uid for uid, _ in rows] == ["1", "4"] and more
    assert rows[0][1] == LATEST
    with pytest.raises(ValueError, match="no 'data' list"):
        sportmonks.finished_fixtures({})


def test_listing_window_looks_back_and_caps_the_span() -> None:
    assert results.listing_window(LATEST, NOW) == (date(2026, 9, 30), date(2026, 10, 10))
    start, end = results.listing_window(LATEST - timedelta(days=60), NOW)
    assert end - start == results.MAX_SPAN
    with pytest.raises(ValueError, match="backfill"):
        results.listing_window(None, NOW)


def test_the_window_starts_behind_the_league_furthest_behind() -> None:
    # 2026-10-21: the pilot database held the Scottish Premiership to 18 Oct (free plan) but Brazil only to 11 Oct; a
    # database-wide latest would have listed from 15 Oct and never fetched Brazil's matches of 11-15 Oct.
    brazil, scotland = datetime(2026, 10, 11, tzinfo=timezone.utc), datetime(2026, 10, 18, tzinfo=timezone.utc)
    assert results.earliest_latest({"Brazil Serie A": brazil, "Scottish Premiership": scotland},
                                   ["Brazil Serie A", "Scottish Premiership"]) == brazil
    assert results.earliest_latest({}, ["Brazil Serie A"]) is None
    with pytest.raises(ValueError, match="Championship: backfill"):
        results.earliest_latest({"Brazil Serie A": brazil}, ["Brazil Serie A", "Championship"])


def test_missing_fixtures_skips_stored_finals_and_runs_earliest_first() -> None:
    listed = [("3", LATEST + timedelta(days=2)), ("1", LATEST), ("2", LATEST + timedelta(days=1))]
    assert results.missing_fixtures(listed, frozenset({"2"})) == ["1", "3"]


class _Listing:
    """Sportmonks' fixture listing: `pages` pages of one finished League Two match each."""

    def __init__(self, pages: int) -> None:
        self.pages, self.calls = pages, 0
        self.paths: list[str] = []

    def get_json(self, path: str) -> dict[str, Any]:
        self.paths.append(path)
        assert "fixtures/between/2026-09-30/2026-10-10" in path and "fixtureLeagues:12,14&" in path
        self.calls += 1
        return {"data": [_row(100 + self.calls)], "pagination": {"has_more": self.calls < self.pages}}


def test_a_listing_longer_than_the_page_limit_ingests_nothing() -> None:
    assert [uid for uid, _ in results.list_finished(_Listing(2), date(2026, 9, 30), date(2026, 10, 10))] == ["101", "102"]
    with pytest.raises(ValueError, match="more than"):
        results.list_finished(_Listing(results.MAX_LISTING_PAGES + 1), date(2026, 9, 30), date(2026, 10, 10))


def test_run_ingests_only_the_missing_fixtures_and_reports_failures(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setattr(results, "read_stored", lambda dsn, names: (LATEST, frozenset({_uid(101)}))
                        if names == ["League One", "League Two"] else pytest.fail(f"read_stored got {names}"))
    sent: list[list[str]] = []

    def fake_ingest(client: Any, dsn: str, uids: list[str], workers: int) -> SlateReport:
        sent.append(list(uids))
        report = SlateReport(job="stats", results={
            _uid(102): FixtureResult(_uid(102), True, 30, 1, 0.1),
            _uid(103): FixtureResult(_uid(103), False, 0, 0, 0.1, error="ValueError: refused"),
        })
        report.merge = MergeResult(1, 30, 0)
        return report

    refreshed: list[date] = []
    monkeypatch.setattr(results, "ingest", fake_ingest)

    def refresh_incomplete(dsn: str, since: date) -> int:
        refreshed.append(since)
        return 2

    monkeypatch.setattr(results, "refresh_incomplete", refresh_incomplete)
    refused = tmp_path / "refused.json"
    report = results.run(NOW, _Listing(3), "postgresql://unused", refused_path=refused)
    assert sent == [[_uid(102), _uid(103)]]
    assert refreshed == [report.start - timedelta(days=1)] and report.incomplete == 2  # quarantine refreshed
    assert "2 club-matches in the window have a player without an ID" in report.text()
    assert report.listed == 3 and report.missing == [_uid(102), _uid(103)] and report.ingested == 1
    assert report.failed == {_uid(103): "ValueError: refused"} and not report.ok
    assert f"3 finished, 2 not yet stored, 1 ingested, 1 FAILED ({_uid(103)})" in report.text()
    # The adapter's refusal is permanent: recorded, and not asked for again on the next run (2026-10-15: League One/Two
    # used to retry it on every run and fail the job until the listing window moved past it).
    assert set(results.load_refused(refused)) == {_uid(103)}
    dry = results.run(NOW, _Listing(3), "postgresql://unused", dry_run=True, refused_path=refused)
    assert dry.missing == [_uid(102)] and dry.skipped_refused == 1 and dry.ingested == 0 and len(sent) == 1
    assert "2 not yet stored" not in dry.text() and "(1 refused earlier, not retried)" in dry.text()


def test_every_results_run_names_its_refusal_record(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """League One/Two reads and writes REFUSED_FILE, the pilot PILOT_REFUSED_FILE; run() has no default, so a caller
    cannot leave the record out again."""
    from prop_alpha.ingestion import pipeline

    with pytest.raises(TypeError, match="refused_path"):
        results.run(NOW, _Listing(1), "postgresql://unused", dry_run=True)  # type: ignore[call-arg]

    closed: list[bool] = []

    class _Client:
        def close(self) -> None:
            closed.append(True)

    seen: list[Any] = []

    def fake_run(now: datetime, client: Any, dsn: str, **kwargs: Any) -> results.ResultsReport:
        seen.append((kwargs["refused_path"], kwargs.get("leagues", sportmonks.SCAN_LEAGUES)))
        return results.ResultsReport(date(2026, 10, 1), date(2026, 10, 10))

    monkeypatch.setattr(pipeline, "build_vendor_client", lambda environ: _Client())
    monkeypatch.setattr(results, "pilot_leagues", lambda client: (PILOT, []))
    monkeypatch.setattr(results, "run", fake_run)
    monkeypatch.setattr(results, "RESULTS_OK_FILE", tmp_path / "results_last_ok.json")  # never the real state file
    assert results.main([]) == 0 and results.main(["--pilot"]) == 0
    assert seen == [(results.REFUSED_FILE, sportmonks.SCAN_LEAGUES), (results.PILOT_REFUSED_FILE, PILOT)]
    assert results.REFUSED_FILE != results.PILOT_REFUSED_FILE
    assert closed == [True, True]  # the client is closed after each run


PILOT = {648: "Brazil Serie A", 501: "Scottish Premiership", 9: "Championship"}


class _PilotListing:
    """A Sportmonks stub: the leagues listing in two pages (the plan's three pilot leagues), and every fixture listing with
    one finished match in Brazil Serie A, one in the Scottish Premiership and one in League Two (not a pilot league)."""

    def __init__(self) -> None:
        self.paths: list[str] = []

    def get_json(self, path: str) -> dict[str, Any]:
        self.paths.append(path)
        if path.startswith("leagues?"):
            if path.endswith("page=1"):
                return {"data": [{"id": 648, "name": "Serie A", "country": {"name": "Brazil"}},
                                 {"id": 9, "name": "Championship", "country": {"name": "England"}}],
                        "pagination": {"has_more": True}}
            return {"data": [{"id": 501, "name": "Premiership", "country": {"name": "Scotland"}}],
                    "pagination": {"has_more": False}}
        return {"data": [_row(1, 648), _row(2, 501), _row(3, 14)], "pagination": {"has_more": False}}


class _FreePlan(_PilotListing):
    """The free plan seen on 2026-10-21: the Scottish Premiership only among the pilot leagues."""

    def get_json(self, path: str) -> dict[str, Any]:
        return {"data": [{"id": 501, "name": "Premiership", "country": {"name": "Scotland"}}],
                "pagination": {"has_more": False}}


def test_the_pilot_leagues_are_read_from_the_plan_across_pages_and_missing_ones_are_named() -> None:
    client = _PilotListing()
    assert results.pilot_leagues(client) == (PILOT, [])
    assert [p.rsplit("page=", 1)[1] for p in client.paths] == ["1", "2"]
    assert sorted(sportmonks.PILOT_LEAGUES_WANTED.values()) == ["Brazil Serie A", "Championship", "Scottish Premiership"]
    assert results.pilot_leagues(_FreePlan()) == ({501: "Scottish Premiership"}, ["Brazil Serie A", "Championship"])


def test_the_results_job_lists_and_ingests_only_the_pilot_leagues_when_given_them(monkeypatch: pytest.MonkeyPatch,
                                                                                  tmp_path: Any) -> None:
    monkeypatch.setattr(results, "read_stored", lambda dsn, names: (LATEST, frozenset({_uid(1)})))
    client = _PilotListing()
    report = results.run(NOW, client, "postgresql://pilot", dry_run=True, leagues=PILOT,
                         refused_path=tmp_path / "pilot_refused.json")
    assert report.listed == 2 and report.missing == [_uid(2)]  # League Two is not the pilot's
    assert all("fixtureLeagues:9,501,648&" in path for path in client.paths)
    default = results.run(NOW, _PilotListing(), "postgresql://working", dry_run=True, refused_path=tmp_path / "refused.json")
    assert default.listed == 1 and default.missing == [_uid(3)]


def test_vendor_refusals_are_recorded_once_and_never_network_errors(tmp_path: Any) -> None:
    path = tmp_path / "pilot_refused.json"
    assert results.load_refused(path) == {}
    failed = {"1": "ValueError: fixture 1: no ['TACKLES'] in any lineup", "2": "ConnectionError: read timed out",
              "3": "timed out", "4": 'HTTPError: HTTP 404: {"error":{"code":"STATS_NOT_AVAILABLE_AT_SOURCE"}}'}
    # A refusal and a match TheStatsAPI holds no statistics for are permanent; a network error is retried.
    assert results.record_refused(path, failed) == 2 and sorted(results.load_refused(path)) == ["1", "4"]
    assert results.record_refused(path, failed) == 0  # recorded once
    path.write_text("[1]", encoding="utf-8")
    with pytest.raises(ValueError, match="not a refusal record"):
        results.load_refused(path)


def test_the_pilot_results_job_skips_recorded_refusals_and_records_new_ones(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setattr(results, "read_stored", lambda dsn, names: (LATEST, frozenset({_uid(1)})))
    path = tmp_path / "pilot_refused.json"
    path.write_text('{"%s": "ValueError: refused earlier"}' % _uid(2), encoding="utf-8")
    sent: list[list[str]] = []

    def fake_ingest(client: Any, dsn: str, uids: list[str], workers: int) -> SlateReport:
        sent.append(list(uids))
        return SlateReport(job="stats", results={u: FixtureResult(u, False, 0, 0, 0.1, error="ValueError: new defect")
                                                  for u in uids})

    monkeypatch.setattr(results, "ingest", fake_ingest)
    monkeypatch.setattr(results, "refresh_incomplete", lambda dsn, since: 0)
    report = results.run(NOW, _PilotListing(), "postgresql://pilot", leagues=PILOT, refused_path=path)
    assert sent == []  # match 2 is the only pilot match not stored and it was refused earlier: nothing is ingested
    assert report.missing == [] and set(results.load_refused(path)) == {_uid(2)}


def test_a_match_with_no_statistics_at_the_source_is_recorded_without_failing_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """2026-10-15: the first pilot run on TheStatsAPI failed (and raised a TASK FAILING alert) over one Scottish match the
    source holds no statistics for. Such a match is recorded and reported, not a failure; an adapter refusal still is."""
    monkeypatch.setattr(results, "read_stored", lambda dsn, names: (LATEST, frozenset({_uid(101)})))
    missing_stats = 'HTTPError: HTTP 404: {"error":{"code":"STATS_NOT_AVAILABLE_AT_SOURCE"}}'

    def fake_ingest(client: Any, dsn: str, uids: list[str], workers: int) -> SlateReport:
        return SlateReport(job="stats", results={_uid(102): FixtureResult(_uid(102), False, 0, 0, 0.1, error=missing_stats)})

    monkeypatch.setattr(results, "ingest", fake_ingest)
    monkeypatch.setattr(results, "refresh_incomplete", lambda dsn, since: 0)
    refused = tmp_path / "refused.json"
    report = results.run(NOW, _Listing(2), "postgresql://unused", refused_path=refused)
    assert report.ok and report.failed == {} and report.unavailable == [_uid(102)]
    assert set(results.load_refused(refused)) == {_uid(102)}
    assert f"1 with no statistics at the source, recorded and not retried ({_uid(102)})" in report.text()
    assert "FAILED" not in report.text()
