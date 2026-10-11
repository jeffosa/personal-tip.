"""Results job (ingestion/results.py): list finished League One/Two matches and ingest the ones not yet stored."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

from prop_alpha.ingestion import results, sportmonks
from prop_alpha.ingestion.pipeline import FixtureResult, MergeResult, SlateReport

NOW = datetime(2026, 10, 10, 20, 0, tzinfo=timezone.utc)
LATEST = datetime(2026, 10, 3, 14, 0, tzinfo=timezone.utc)


def _row(fid: int, league: int, state: str, kickoff: datetime) -> dict[str, Any]:
    return {"id": fid, "league_id": league, "state": {"developer_name": state},
            "starting_at_timestamp": int(kickoff.timestamp()),
            "participants": [{"id": 76, "name": "Walsall", "meta": {"location": "home"}},
                             {"id": 348, "name": "Port Vale", "meta": {"location": "away"}}]}


def test_finished_listing_keeps_final_scan_league_fixtures() -> None:
    body = {"data": [_row(1, 14, "FT", LATEST), _row(2, 14, "POSTPONED", LATEST), _row(3, 8, "FT", LATEST),
                     _row(4, 12, "AET", LATEST + timedelta(days=1)), _row(5, 12, "NS", NOW)],
            "pagination": {"has_more": True}}
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


def test_missing_fixtures_skips_stored_finals_and_runs_earliest_first() -> None:
    listed = [("3", LATEST + timedelta(days=2)), ("1", LATEST), ("2", LATEST + timedelta(days=1))]
    assert results.missing_fixtures(listed, frozenset({"2"})) == ["1", "3"]


class _Listing:
    def __init__(self, pages: int) -> None:
        self.pages, self.calls = pages, 0

    def get_json(self, path: str) -> dict[str, Any]:
        self.calls += 1
        assert "fixtures/between/2026-09-30/2026-10-10" in path
        return {"data": [_row(100 + self.calls, 14, "FT", LATEST)], "pagination": {"has_more": self.calls < self.pages}}


def test_a_listing_longer_than_the_page_limit_ingests_nothing() -> None:
    assert len(results.list_finished(_Listing(2), date(2026, 9, 30), date(2026, 10, 10))) == 2
    with pytest.raises(ValueError, match="more than"):
        results.list_finished(_Listing(results.MAX_LISTING_PAGES + 1), date(2026, 9, 30), date(2026, 10, 10))


def test_run_ingests_only_the_missing_fixtures_and_reports_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(results, "read_stored", lambda dsn: (LATEST, frozenset({"101"})))
    sent: list[list[str]] = []

    def fake_ingest(client: Any, dsn: str, uids: list[str], workers: int) -> SlateReport:
        sent.append(list(uids))
        report = SlateReport(job="stats", results={
            "102": FixtureResult("102", True, 30, 1, 0.1),
            "103": FixtureResult("103", False, 0, 0, 0.1, error="ValueError: refused"),
        })
        report.merge = MergeResult(1, 30, 0)
        return report

    refreshed: list[date] = []
    monkeypatch.setattr(results, "ingest", fake_ingest)
    def refresh_incomplete(dsn: str, since: date) -> int:
        refreshed.append(since)
        return 2

    monkeypatch.setattr(results, "refresh_incomplete", refresh_incomplete)
    report = results.run(NOW, _Listing(3), "postgresql://unused")
    assert sent == [["102", "103"]]
    assert refreshed == [report.start - timedelta(days=1)] and report.incomplete == 2  # quarantine refreshed
    assert "2 club-matches in the window have a player without an ID" in report.text()
    assert report.listed == 3 and report.missing == ["102", "103"] and report.ingested == 1
    assert report.failed == {"103": "ValueError: refused"} and not report.ok
    assert "3 finished, 2 not yet stored, 1 ingested, 1 FAILED (103)" in report.text()
    dry = results.run(NOW, _Listing(3), "postgresql://unused", dry_run=True)
    assert dry.missing == ["102", "103"] and dry.ingested == 0 and len(sent) == 1


PILOT = {648: "Brazil Serie A", 501: "Scottish Premiership"}


class _PilotListing:
    """A Sportmonks stub that answers the leagues listing in two pages and every fixture listing with one finished
    match in a pilot league, one in League One and one in the Premier League (not wanted)."""

    def __init__(self) -> None:
        self.paths: list[str] = []

    def get_json(self, path: str) -> dict[str, Any]:
        self.paths.append(path)
        if path.startswith("leagues?"):
            if path.endswith("page=1"):
                return {"data": [{"id": 648, "name": "Serie A", "country": {"name": "Brazil"}}],
                        "pagination": {"has_more": True}}
            return {"data": [{"id": 501, "name": "Premiership", "country": {"name": "Scotland"}}],
                    "pagination": {"has_more": False}}
        return {"data": [_row(1, 648, "FT", LATEST), _row(2, 501, "FT", LATEST), _row(3, 12, "FT", LATEST),
                         _row(4, 8, "FT", LATEST)], "pagination": {"has_more": False}}


def test_the_pilot_leagues_are_resolved_across_pages_and_missing_ones_are_named() -> None:
    client = _PilotListing()
    assert results.pilot_leagues(client) == (PILOT, ["Scottish Championship"])
    assert [p.rsplit("page=", 1)[1] for p in client.paths] == ["1", "2"]


def test_the_results_job_lists_and_ingests_only_the_pilot_leagues_when_given_them(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(results, "read_stored", lambda dsn: (LATEST, frozenset({"1"})))
    client = _PilotListing()
    report = results.run(NOW, client, "postgresql://pilot", dry_run=True, leagues=PILOT)
    assert report.listed == 2 and report.missing == ["2"]  # League One and the Premier League are not the pilot's
    assert all("fixtureLeagues:501,648&" in path for path in client.paths)
    default = results.run(NOW, client, "postgresql://working", dry_run=True)
    assert default.listed == 1 and any("fixtureLeagues:12,14&" in path for path in client.paths)


def test_vendor_refusals_are_recorded_once_and_never_network_errors(tmp_path: Any) -> None:
    path = tmp_path / "pilot_refused.json"
    assert results.load_refused(path) == {}
    failed = {"1": "ValueError: fixture 1: no ['TACKLES'] in any lineup", "2": "ConnectionError: read timed out",
              "3": "timed out"}
    assert results.record_refused(path, failed) == 1 and list(results.load_refused(path)) == ["1"]
    assert results.record_refused(path, failed) == 0  # recorded once
    path.write_text("[1]", encoding="utf-8")
    with pytest.raises(ValueError, match="not a refusal record"):
        results.load_refused(path)


def test_the_pilot_results_job_skips_recorded_refusals_and_records_new_ones(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setattr(results, "read_stored", lambda dsn: (LATEST, frozenset({"1"})))
    path = tmp_path / "pilot_refused.json"
    path.write_text('{"2": "ValueError: refused earlier"}', encoding="utf-8")
    sent: list[list[str]] = []

    def fake_ingest(client: Any, dsn: str, uids: list[str], workers: int) -> SlateReport:
        sent.append(list(uids))
        return SlateReport(job="stats", results={u: FixtureResult(u, False, 0, 0, 0.1, error="ValueError: new defect")
                                                  for u in uids})

    monkeypatch.setattr(results, "ingest", fake_ingest)
    monkeypatch.setattr(results, "refresh_incomplete", lambda dsn, since: 0)
    report = results.run(NOW, _PilotListing(), "postgresql://pilot", leagues=PILOT, refused_path=path)
    assert sent == []  # "2" is the only pilot match not stored and it was refused earlier: nothing is ingested
    assert report.missing == [] and set(results.load_refused(path)) == {"2"}
