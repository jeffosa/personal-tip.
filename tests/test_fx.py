"""Bankroll currency and the book-currency exchange rate (alerts/fx.py; maintainer decision 2026-10-04)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import requests

from prop_alpha.alerts import fx
from prop_alpha.alerts.fx import ExchangeRate
from prop_alpha.alerts.telegram import DEFAULT_CRITERIA, demo_case, format_digest, format_directive
from prop_alpha.scanner.value_screen import assess_high_value

TODAY = date(2026, 10, 4)
RATE = ExchangeRate("GBP", "USD", 1.3201, date(2026, 10, 2))


def test_bankroll_currency_is_required_and_must_be_supported(tmp_path: Path) -> None:
    no_file = tmp_path / "missing.env"
    with pytest.raises(ValueError, match="PROP_ALPHA_CURRENCY is not set"):
        fx.bankroll_currency({}, no_file)
    with pytest.raises(ValueError, match="not supported"):
        fx.bankroll_currency({"PROP_ALPHA_CURRENCY": "EUR"}, no_file)
    assert fx.bankroll_currency({"PROP_ALPHA_CURRENCY": " usd "}, no_file) == "USD"
    env_file = tmp_path / ".env"
    env_file.write_text("PROP_ALPHA_CURRENCY=GBP\n", encoding="utf-8")
    assert fx.bankroll_currency({}, env_file) == "GBP"


def test_rates_are_validated() -> None:
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="finite and positive"):
            ExchangeRate("GBP", "USD", bad, TODAY)
    with pytest.raises(ValueError, match="supported"):
        ExchangeRate("GBP", "JPY", 190.0, TODAY)
    with pytest.raises(ValueError, match="same-currency"):
        ExchangeRate("USD", "USD", 1.1, TODAY)
    assert RATE.convert(50.0) == pytest.approx(66.005) and not RATE.identity and fx.identity("GBP").identity


def test_parse_rate_reads_the_frankfurter_reply_and_refuses_anything_else() -> None:
    body = {"amount": 1.0, "base": "GBP", "date": "2026-10-02", "rates": {"USD": 1.3201}}
    assert fx.parse_rate(body, "GBP", "USD") == RATE
    bad: Any
    for bad in ({"base": "EUR", "date": "2026-10-02", "rates": {"USD": 1.1}}, {"base": "GBP", "rates": {}},
                {"base": "GBP", "date": "2026-10-02", "rates": {"USD": "1.32"}}, []):
        with pytest.raises(ValueError):
            fx.parse_rate(bad, "GBP", "USD")


def test_fetch_rate_refuses_an_error_reply() -> None:
    class Session:
        def __init__(self, status: int) -> None:
            self.status = status

        def get(self, url: str, timeout: float) -> Any:
            assert "base=GBP" in url and "symbols=USD" in url
            response = requests.Response()
            response.status_code = self.status
            response._content = b'{"base": "GBP", "date": "2026-10-02", "rates": {"USD": 1.3201}}'
            return response

    assert fx.fetch_rate("GBP", "USD", Session(200)) == RATE
    with pytest.raises(requests.HTTPError, match="HTTP 503"):
        fx.fetch_rate("GBP", "USD", Session(503))


def test_cached_rate_must_exist_match_and_be_fresh(tmp_path: Path) -> None:
    path = tmp_path / "fx_rate.json"
    assert fx.book_to_bankroll("GBP", TODAY, path).identity  # same currency: no rate needed
    with pytest.raises(ValueError, match="no GBP->USD rate cached"):
        fx.book_to_bankroll("USD", TODAY, path)
    fx.save_rate(RATE, path)
    assert fx.cached_rate(path) == RATE and fx.book_to_bankroll("USD", TODAY, path) == RATE
    with pytest.raises(ValueError, match="older than 5 days"):
        fx.book_to_bankroll("USD", RATE.as_of + fx.MAX_RATE_AGE + timedelta(days=1), path)
    fx.save_rate(ExchangeRate("USD", "GBP", 0.75, TODAY), path)
    with pytest.raises(ValueError, match="not GBP->USD"):
        fx.book_to_bankroll("USD", TODAY, path)
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="not a rate record"):
        fx.cached_rate(path)


def test_alerts_show_the_stake_in_both_currencies_and_the_assumed_limit_converted() -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    projections, report = demo_case(now)
    directive = next(d for d in report.directives if d.line_value == 4.5)
    assessment = assess_high_value(directive, projections[(1001, 55, "tackles")], DEFAULT_CRITERIA,
                                   np.random.default_rng(1))
    from dataclasses import replace

    assumed = replace(directive, limit_published=False)
    in_pounds = f"(= GBP {directive.stake / RATE.rate:.2f} at 1.3201, ECB 02 Oct)"
    for text in (format_directive(assumed, assessment, "USD", book_rate=RATE),
                 format_digest([(assumed, assessment, None)], 1, 1, "", "USD", RATE)):
        assert f"USD {directive.stake:.2f} {in_pounds}" in text
        assert "book limit not published, assumed GBP 50 = USD 66.00" in text
    plain = format_directive(directive, assessment, "GBP")
    assert "(= GBP" not in plain and "assumed" not in plain


def test_a_future_dated_rate_is_refused_not_fresh(tmp_path: Path) -> None:
    path = tmp_path / "fx_rate.json"
    fx.save_rate(ExchangeRate("GBP", "USD", 1.32, TODAY + timedelta(days=1)), path)
    assert not fx.is_fresh(ExchangeRate("GBP", "USD", 1.32, TODAY + timedelta(days=1)), TODAY)
    with pytest.raises(ValueError, match="dated after today"):
        fx.book_to_bankroll("USD", TODAY, path)
