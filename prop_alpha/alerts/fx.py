"""Bankroll currency and the exchange rate from the bookmakers' currency (maintainer decision 2026-10-04: bankroll
in USD, books in GBP).

The bankroll currency is PROP_ALPHA_CURRENCY (required, in the environment or .env). Book limits, including
oddspapi.UNKNOWN_LIMIT_STAKE, are taken to be in BOOK_CURRENCY (assumed: the scanned books are UK books; not yet
checked against an account). When the two differ, every limit is converted into the bankroll currency at the
European Central Bank reference rate before sizing, and every stake is shown in both currencies.

    python -m prop_alpha.alerts.fx --refresh    # fetch today's rate into the cache (the 4-hourly task runs this)
    python -m prop_alpha.alerts.fx              # print the cached rate and whether it is fresh enough

I/O: one HTTPS GET to RATE_URL (Frankfurter, which serves the ECB reference rates; no key) and the gitignored
cache file .prop_alpha_state/fx_rate.json. The scan and the ledger read only the cache, so a rate-source outage
shorter than MAX_RATE_AGE does not stop them; an older or missing rate makes the scan refuse to run.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final, Mapping

import requests

from prop_alpha.config import DEFAULT_ENV_FILE, credential

CURRENCY_ENV: Final[str] = "PROP_ALPHA_CURRENCY"
BOOK_CURRENCY: Final[str] = "GBP"
SUPPORTED_CURRENCIES: Final[frozenset[str]] = frozenset({"GBP", "USD"})
# Reply stake prefixes (alerts/replies.py) and the currency each means.
CURRENCY_SYMBOLS: Final[Mapping[str, str]] = {"£": "GBP", "$": "USD", "€": "EUR"}
RATE_URL: Final[str] = "https://api.frankfurter.dev/v1/latest?base={base}&symbols={quote}"
# ECB rates are published on TARGET working days; 5 days covers a long weekend with a holiday.
MAX_RATE_AGE: Final[timedelta] = timedelta(days=5)
DEFAULT_RATE_FILE: Final[Path] = DEFAULT_ENV_FILE.parent / ".prop_alpha_state" / "fx_rate.json"
REQUEST_TIMEOUT_S: Final[float] = 15.0


def bankroll_currency(environ: Mapping[str, str] | None = None, env_file: Path = DEFAULT_ENV_FILE) -> str:
    """PROP_ALPHA_CURRENCY from the environment or .env; raises if it is missing or not supported."""
    value = credential(CURRENCY_ENV, environ, env_file).strip().upper()
    if value not in SUPPORTED_CURRENCIES:
        raise ValueError(f"{CURRENCY_ENV}={value!r} is not supported (only {', '.join(sorted(SUPPORTED_CURRENCIES))})")
    return value


@dataclass(frozen=True)
class ExchangeRate:
    """1 `base` = `rate` `quote`, the reference rate published for `as_of`."""

    base: str
    quote: str
    rate: float
    as_of: date
    source: str = "ECB"

    def __post_init__(self) -> None:
        if not (math.isfinite(self.rate) and self.rate > 0.0):
            raise ValueError("exchange rate must be finite and positive")
        if self.base not in SUPPORTED_CURRENCIES or self.quote not in SUPPORTED_CURRENCIES:
            raise ValueError("exchange rate currencies must be supported")
        if self.base == self.quote and self.rate != 1.0:
            raise ValueError("a same-currency rate is 1")

    @property
    def identity(self) -> bool:
        return self.base == self.quote

    def convert(self, amount: float) -> float:
        """`amount` in base -> quote."""
        return amount * self.rate

    def describe(self) -> str:
        return f"{self.base}->{self.quote} {self.rate:.4f} ({self.source} {self.as_of:%d %b})"


def identity(currency: str) -> ExchangeRate:
    return ExchangeRate(currency, currency, 1.0, date(1970, 1, 1), "same currency")


def parse_rate(body: Any, base: str, quote: str) -> ExchangeRate:
    """The rate in a Frankfurter reply ({"base": "GBP", "date": "2026-10-02", "rates": {"USD": 1.3201}})."""
    if not isinstance(body, Mapping) or body.get("base") != base or not isinstance(body.get("rates"), Mapping):
        raise ValueError(f"rate reply is not a {base} rate")
    value = body["rates"].get(quote)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"rate reply has no {quote} rate")
    return ExchangeRate(base, quote, float(value), date.fromisoformat(str(body.get("date"))))


def fetch_rate(base: str, quote: str, session: Any = requests) -> ExchangeRate:
    response = session.get(RATE_URL.format(base=base, quote=quote), timeout=REQUEST_TIMEOUT_S)
    if response.status_code != 200:
        raise requests.HTTPError(f"rate source answered HTTP {response.status_code}")
    return parse_rate(response.json(), base, quote)


def save_rate(rate: ExchangeRate, path: Path = DEFAULT_RATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"base": rate.base, "quote": rate.quote, "rate": rate.rate,
                                "as_of": rate.as_of.isoformat(), "source": rate.source}), encoding="utf-8")


def cached_rate(path: Path = DEFAULT_RATE_FILE) -> ExchangeRate | None:
    """The cached rate, or None when nothing is cached yet. A corrupt cache raises."""
    if not path.is_file():
        return None
    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name} is not a rate record")
    return ExchangeRate(str(raw["base"]), str(raw["quote"]), float(raw["rate"]), date.fromisoformat(str(raw["as_of"])),
                        str(raw.get("source", "ECB")))


def is_fresh(rate: ExchangeRate, today: date) -> bool:
    """Published at most MAX_RATE_AGE before `today`, and not after it: a future date means a corrupt cache or a
    wrong clock, so it is refused rather than trusted (audit 2026-10-04)."""
    return rate.identity or timedelta(0) <= today - rate.as_of <= MAX_RATE_AGE


def book_to_bankroll(currency: str, today: date, path: Path = DEFAULT_RATE_FILE) -> ExchangeRate:
    """The rate from BOOK_CURRENCY into the bankroll `currency`: identity when they match, else the cached rate,
    which must be fresh. Raises when it is missing, for other currencies, or older than MAX_RATE_AGE."""
    if currency == BOOK_CURRENCY:
        return identity(currency)
    rate = cached_rate(path)
    if rate is None:
        raise ValueError(f"no {BOOK_CURRENCY}->{currency} rate cached: run python -m prop_alpha.alerts.fx --refresh")
    if (rate.base, rate.quote) != (BOOK_CURRENCY, currency):
        raise ValueError(f"cached rate is {rate.base}->{rate.quote}, not {BOOK_CURRENCY}->{currency}")
    if rate.as_of > today:
        raise ValueError(f"cached rate {rate.describe()} is dated after today ({today}); refresh it")
    if not is_fresh(rate, today):
        raise ValueError(f"cached rate {rate.describe()} is older than {MAX_RATE_AGE.days} days; refresh it")
    return rate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bankroll currency and the book-currency exchange rate.")
    parser.add_argument("--refresh", action="store_true", help="fetch the current rate into the cache")
    args = parser.parse_args(argv)
    currency = bankroll_currency()
    today = datetime.now(timezone.utc).date()
    if currency == BOOK_CURRENCY:
        print(f"bankroll in {currency}, books in {BOOK_CURRENCY}: no conversion")
        return 0
    if args.refresh:
        save_rate(fetch_rate(BOOK_CURRENCY, currency))
    rate = cached_rate()
    if rate is None:
        print(f"no {BOOK_CURRENCY}->{currency} rate cached")
        return 1
    print(f"{rate.describe()}{'' if is_fresh(rate, today) else ' STALE'}")
    return 0 if is_fresh(rate, today) else 1


if __name__ == "__main__":
    sys.exit(main())
