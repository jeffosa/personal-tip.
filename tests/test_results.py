"""Results job (ingestion/results.py): list finished League One/Two matches and ingest the ones not yet stored. The data
vendor is TheStatsAPI since 2026-10-15 (it was Sportmonks)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

import pytest

from prop_alpha.ingestion import results, thestatsapi
from prop_alpha.ingestion.pipeline import MAX_TSA_PAGES, FixtureResult, MergeResult, SlateReport

NOW = datetime(2026, 10, 10, 20, 0, tzinfo=timezone.utc)
LATEST = datetime(2026, 10, 3, 14, 0, tzinfo=timezone.utc)


def _row(number: int, competition: str = "comp_4023", kickoff: datetime = LATEST) -> dict[str, Any]:
    """A finished TheStatsAPI match listing row."""
    return {"id": thestatsapi.match_uid(number), "competition_id": competition, "status": "finished",
            "utc_date": kickoff.isoformat().replace("+00:00", "Z"),
            "home_team": {"id": "76", "name": "Walsall"}, "away_team": {"id": "348", "name": "Port Vale"}}


def _uid(number: int) -> str:
    return thestatsapi.match_uid(number)


def test_listing_window_looks_back_and_caps_the_span() -> None:
    assert results.listing_window(LATEST, NOW) == (date(2026, 9, 30), date(2026, 10, 10))
    start, end = results.listing_window(LATEST - timedelta(days=60), NOW)
    assert end - start == results.MAX_SPAN
    with pytest.raises(ValueError, match="backfill"):
        results.listing_window(None, NOW)


def test_missing_fixtures_skips_stored_finals_and_runs_earliest_first() -> None:
    listed = [("3", LATEST + timedelta(days=2)), ("1", LATEST), ("2", LATEST + timedelta(days=1))]
    assert results.missing_fixtures(listed, frozenset({"2"})) == ["1", "3"]


class _Listing:
    """TheStatsAPI's finished-match listing: `pages` pages of one League Two match each, nothing elsewhere."""

    def __init__(self, pages: int) -> None:
        self.pages, self.calls = pages, 0
        self.paths: list[str] = []

    def get_json(self, path: str) -> dict[str, Any]:
        self.paths.append(path)
        assert "date_from=2026-09-30&date_to=2026-10-10" in path and "status=finished" in path
        if "competition_id=comp_4023" not in path:
            return {"data": [], "meta": {"total_pages": 0}}
        self.calls += 1
        return {"data": [_row(100 + self.calls)], "meta": {"total_pages": self.pages}}


def test_the_finished_listing_asks_for_finished_matches_of_each_scan_competition() -> None:
    client = _Listing(2)
    rows = results.list_finished(client, date(2026, 9, 30), date(2026, 10, 10))
    assert [r["id"] for r in rows] == [_uid(101), _uid(102)]
    assert {p.split("competition_id=")[1].split("&")[0] for p in client.paths} == set(thestatsapi.SCAN_COMPETITIONS)


def test_a_listing_longer_than_the_page_limit_ingests_nothing() -> None:
    with pytest.raises(ValueError, match="more than"):
        results.list_finished(_Listing(MAX_TSA_PAGES + 1), date(2026, 9, 30), date(2026, 10, 10))


def test_run_ingests_only_the_missing_fixtures_and_reports_failures(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setattr(results, "read_stored", lambda dsn: (LATEST, frozenset({_uid(101)})))
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
    listing: dict[str, Mapping[str, Any]] = {}
    refused = tmp_path / "refused.json"
    report = results.run(NOW, _Listing(3), "postgresql://unused", refused_path=refused, listing=listing)
    assert sent == [[_uid(102), _uid(103)]]
    assert sorted(listing) == [_uid(101), _uid(102), _uid(103)]  # the client's match table: teams, kickoff, score
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


def test_every_results_run_names_its_refusal_record(monkeypatch: pytest.MonkeyPatch) -> None:
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
        seen.append((kwargs["refused_path"], kwargs.get("leagues", thestatsapi.SCAN_COMPETITIONS)))
        return results.ResultsReport(date(2026, 10, 1), date(2026, 10, 10))

    monkeypatch.setattr(pipeline, "build_thestatsapi_client", lambda environ, listing=None: _Client())
    monkeypatch.setattr(results, "run", fake_run)
    assert results.main([]) == 0 and results.main(["--pilot"]) == 0
    assert seen == [(results.REFUSED_FILE, thestatsapi.SCAN_COMPETITIONS),
                    (results.PILOT_REFUSED_FILE, thestatsapi.PILOT_COMPETITIONS)]
    assert results.REFUSED_FILE != results.PILOT_REFUSED_FILE
    assert closed == [True, True]  # the client is closed after each run


class _PilotListing:
    """TheStatsAPI's listing: one finished match each in Brazil Serie A, the Scottish Premiership and League One."""

    def __init__(self) -> None:
        self.paths: list[str] = []

    def get_json(self, path: str) -> dict[str, Any]:
        self.paths.append(path)
        by_competition = {"comp_4795": 1, "comp_6387": 2, "comp_0196": 3}
        found = [_row(n, c) for c, n in by_competition.items() if f"competition_id={c}&" in path]
        return {"data": found, "meta": {"total_pages": 1}}


def test_the_pilot_competitions_are_the_three_pilot_leagues() -> None:
    assert sorted(thestatsapi.PILOT_COMPETITIONS.values()) == ["Brazil Serie A", "Scottish Championship",
                                                               "Scottish Premiership"]


def test_the_results_job_lists_and_ingests_only_the_pilot_leagues_when_given_them(monkeypatch: pytest.MonkeyPatch,
                                                                                  tmp_path: Any) -> None:
    monkeypatch.setattr(results, "read_stored", lambda dsn: (LATEST, frozenset({_uid(1)})))
    client = _PilotListing()
    report = results.run(NOW, client, "postgresql://pilot", dry_run=True, leagues=thestatsapi.PILOT_COMPETITIONS,
                         refused_path=tmp_path / "pilot_refused.json")
    assert report.listed == 2 and report.missing == [_uid(2)]  # League One is not the pilot's
    assert {p.split("competition_id=")[1].split("&")[0] for p in client.paths} == set(thestatsapi.PILOT_COMPETITIONS)
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
    monkeypatch.setattr(results, "read_stored", lambda dsn: (LATEST, frozenset({_uid(1)})))
    path = tmp_path / "pilot_refused.json"
    path.write_text('{"%s": "ValueError: refused earlier"}' % _uid(2), encoding="utf-8")
    sent: list[list[str]] = []

    def fake_ingest(client: Any, dsn: str, uids: list[str], workers: int) -> SlateReport:
        sent.append(list(uids))
        return SlateReport(job="stats", results={u: FixtureResult(u, False, 0, 0, 0.1, error="ValueError: new defect")
                                                  for u in uids})

    monkeypatch.setattr(results, "ingest", fake_ingest)
    monkeypatch.setattr(results, "refresh_incomplete", lambda dsn, since: 0)
    report = results.run(NOW, _PilotListing(), "postgresql://pilot", leagues=thestatsapi.PILOT_COMPETITIONS,
                         refused_path=path)
    assert sent == []  # match 2 is the only pilot match not stored and it was refused earlier: nothing is ingested
    assert report.missing == [] and set(results.load_refused(path)) == {_uid(2)}


def test_a_match_with_no_statistics_at_the_source_is_recorded_without_failing_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """2026-10-15: the first pilot run on TheStatsAPI failed (and raised a TASK FAILING alert) over one Scottish match the
    source holds no statistics for. Such a match is recorded and reported, not a failure; an adapter refusal still is."""
    monkeypatch.setattr(results, "read_stored", lambda dsn: (LATEST, frozenset({_uid(101)})))
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
