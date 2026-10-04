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

    monkeypatch.setattr(results, "ingest", fake_ingest)
    report = results.run(NOW, _Listing(3), "postgresql://unused")
    assert sent == [["102", "103"]]
    assert report.listed == 3 and report.missing == ["102", "103"] and report.ingested == 1
    assert report.failed == {"103": "ValueError: refused"} and not report.ok
    assert "3 finished, 2 not yet stored, 1 ingested, 1 FAILED (103)" in report.text()
    dry = results.run(NOW, _Listing(3), "postgresql://unused", dry_run=True)
    assert dry.missing == ["102", "103"] and dry.ingested == 0 and len(sent) == 1
