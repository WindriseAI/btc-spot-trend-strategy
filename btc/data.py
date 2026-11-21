"""Daily closes from public sources, with staleness and cross-source checks.

Primary source is OKX BTC-USDT (the market we trade). A second, independent
source must agree before any order is allowed. A daily bar is only used once
its UTC day is over.
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

from .strategy import Params, Signal, compute_signal

UA = {"User-Agent": "btc-rebalancer/1.0"}
TIMEOUT = 15


class DataError(Exception):
    pass


def _get(url: str, timeout: int = TIMEOUT):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _day(ts: float) -> date:
    return datetime.fromtimestamp(ts, timezone.utc).date()


# Each fetcher returns {UTC date of the bar: close}. It may include the bar of
# the day still in progress; the caller drops it.

def fetch_okx(hostname: str = "www.okx.com") -> dict:
    d = _get(f"https://{hostname}/api/v5/market/candles?instId=BTC-USDT&bar=1Dutc&limit=300")
    if d.get("code") != "0":
        raise DataError(f"okx code {d.get('code')}")
    return {_day(int(r[0]) / 1000): float(r[4]) for r in d["data"] if r[8] == "1"}


def fetch_binance(_: str = "") -> dict:
    err = None
    for host in ("https://data-api.binance.vision", "https://api.binance.com"):
        try:
            rows = _get(f"{host}/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=300")
            return {_day(r[0] / 1000): float(r[4]) for r in rows}
        except Exception as e:  # noqa: BLE001
            err = e
    raise DataError(f"binance: {err}")


def fetch_kraken(_: str = "") -> dict:
    d = _get("https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=1440")
    if d.get("error"):
        raise DataError(f"kraken {d['error']}")
    key = [k for k in d["result"] if k != "last"][0]
    return {_day(r[0]): float(r[4]) for r in d["result"][key]}


def fetch_bitstamp(_: str = "") -> dict:
    d = _get("https://www.bitstamp.net/api/v2/ohlc/btcusd/?step=86400&limit=300")
    return {_day(int(r["timestamp"])): float(r["close"]) for r in d["data"]["ohlc"]}


def fetch_coinbase(_: str = "") -> dict:
    rows = _get("https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=86400")
    return {_day(r[0]): float(r[4]) for r in rows}


# (name, fetcher, quote currency) in order of preference
SOURCES: list = [
    ("OKX", fetch_okx, "USDT"),
    ("Binance", fetch_binance, "USDT"),
    ("Kraken", fetch_kraken, "USD"),
    ("Bitstamp", fetch_bitstamp, "USD"),
    ("Coinbase", fetch_coinbase, "USD"),
]


def longest_window(series: dict, last_day: date, at_least: int) -> list:
    """All consecutive valid closes ending on last_day (oldest first)."""
    out = []
    d = last_day
    while series.get(d) is not None and series[d] == series[d] and series[d] > 0:
        out.append(series[d])
        d -= timedelta(days=1)
    out.reverse()
    return out if len(out) >= at_least else []


def window(series: dict, last_day: date, n: int) -> list:
    """The n closes for the n consecutive days ending on last_day, oldest first."""
    if not series:
        raise DataError("没有数据")
    newest = max(d for d in series if d <= last_day) if any(d <= last_day for d in series) else None
    if newest != last_day:
        raise DataError(f"数据过期：最新完整日线是 {newest}，应为 {last_day}")
    out = []
    for i in range(n - 1, -1, -1):
        d = last_day - timedelta(days=i)
        v = series.get(d)
        if v is None:
            raise DataError(f"缺少 {d} 的日线")
        if not (v == v and v > 0):
            raise DataError(f"{d} 的收盘价无效：{v}")
        out.append(v)
    for a, b in zip(out, out[1:]):
        if abs(b / a - 1) > 0.5:
            raise DataError("日线出现超过 50% 的单日跳变，疑似坏数据")
    return out


@dataclass
class MarketData:
    signal_date: date                       # UTC day whose close the signal uses
    ok_to_trade: bool = False
    closes: Optional[list] = None
    closes_long: Optional[list] = None      # all consecutive closes available (>= closes), for replays
    signal: Optional[Signal] = None
    primary: str = ""
    checker: str = ""
    checker_signal: Optional[Signal] = None
    diff_last: Optional[float] = None       # |primary/checker - 1| on the latest close
    problems: list = field(default_factory=list)   # reasons trading is refused
    notes: list = field(default_factory=list)      # informational
    source_errors: dict = field(default_factory=dict)


def get_market_data(
    now_utc: datetime,
    p: Params = Params(),
    max_diff: float = 0.015,
    hostname: str = "www.okx.com",
    sources: Optional[list] = None,
) -> MarketData:
    """Fetch, validate and cross-check. Never raises for data problems: it
    returns ok_to_trade=False with the reasons in `problems`."""
    last_day = now_utc.date() - timedelta(days=1)
    md = MarketData(signal_date=last_day)
    n = p.min_history
    valid: list = []
    for name, fn, quote in (sources or SOURCES):
        try:
            series = fn(hostname)
            series = {d: v for d, v in series.items() if d <= last_day}   # completed days only
            valid.append((name, window(series, last_day, n), quote, longest_window(series, last_day, n)))
        except Exception as e:  # noqa: BLE001  (network errors, bad payloads, stale data)
            md.source_errors[name] = str(e)[:200]
        if len(valid) == 2:
            break
    if not valid:
        md.problems.append("所有行情数据源都不可用或已过期：" + "；".join(f"{k}: {v}" for k, v in md.source_errors.items()))
        return md
    md.primary, md.closes, _, md.closes_long = valid[0]
    md.signal = compute_signal(md.closes, p)
    if md.source_errors:
        md.notes.append("部分数据源失败：" + "；".join(f"{k}: {v}" for k, v in md.source_errors.items()))
    if len(valid) < 2:
        md.problems.append(f"只有 {md.primary} 一个数据源可用，无法交叉验证，今天不下单")
        return md
    md.checker, c_closes, _, _ = valid[1]
    md.checker_signal = compute_signal(c_closes, p)
    md.diff_last = abs(md.closes[-1] / c_closes[-1] - 1)
    if md.diff_last > max_diff:
        md.problems.append(
            f"{md.primary} 与 {md.checker} 的最新收盘价相差 {md.diff_last*100:.2f}%（上限 {max_diff*100:.1f}%），今天不下单")
    if abs(md.signal.T - md.checker_signal.T) >= p.band:
        md.problems.append(
            f"{md.primary} 与 {md.checker} 算出的目标仓位不一致（{md.signal.T*100:.0f}% 对 {md.checker_signal.T*100:.0f}%），今天不下单")
    elif md.signal.m != md.checker_signal.m:
        md.notes.append(f"{md.checker} 的趋势计数为 {md.checker_signal.m}/{md.checker_signal.n}，与 {md.primary} 略有出入（价格正好在分界线附近），以 {md.primary} 为准")
    md.ok_to_trade = not md.problems
    return md


def okx_server_time(hostname: str = "www.okx.com") -> Optional[datetime]:
    try:
        d = _get(f"https://{hostname}/api/v5/public/time", timeout=10)
        return datetime.fromtimestamp(int(d["data"][0]["ts"]) / 1000, timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def recent_signals(closes: list, last_day: date, days: int, p: Params = Params()) -> list:
    """(day, close, Signal) for the last `days` days that have enough history."""
    out = []
    n = p.min_history
    for back in range(days - 1, -1, -1):
        end = len(closes) - back
        if end >= n:
            out.append((last_day - timedelta(days=back), closes[end - 1], compute_signal(closes[end - n:end], p)))
    return out


Fetcher = Callable[[str], dict]
