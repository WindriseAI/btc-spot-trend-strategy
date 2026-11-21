"""Download historical price data for the backtest into data/ (gitignored).

Sources (all public, no keys):
  yahoo_btc.json     Yahoo Finance BTC-USD daily (2014-09-17 ->), UTC days
  yahoo_eth.json     Yahoo Finance ETH-USD daily (out-of-sample sanity check)
  bitstamp_btc.json  Bitstamp BTC/USD daily OHLC (independent exchange, 2014 ->)
  okx_btc.json       OKX BTC-USDT daily UTC candles (the venue we trade on)
  binance_1h.json    Binance BTCUSDT hourly (2017-08 ->), for fill-delay tests

Usage:  python backtest/fetch_data.py [--force]
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
UA = {"User-Agent": "Mozilla/5.0"}


def get(url: str, tries: int = 5):
    err = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"failed {url}: {err}")


def yahoo(sym: str):
    now = int(time.time())
    d = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1=1410825600&period2={now}&interval=1d")
    r = d["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    out = []
    for i, t in enumerate(r["timestamp"]):
        if q["close"][i] is None:
            continue
        out.append([t, q["open"][i], q["high"][i], q["low"][i], q["close"][i]])
    return out


def bitstamp():
    out = {}
    start = 1388534400  # 2014-01-01
    while True:
        d = get(f"https://www.bitstamp.net/api/v2/ohlc/btcusd/?step=86400&limit=1000&start={start}")
        rows = d["data"]["ohlc"]
        if not rows:
            break
        for x in rows:
            out[int(x["timestamp"])] = [int(x["timestamp"]), float(x["open"]), float(x["high"]), float(x["low"]), float(x["close"])]
        last = int(rows[-1]["timestamp"])
        if len(rows) < 1000 or last <= start:
            break
        start = last + 86400
        time.sleep(0.3)
    return [out[k] for k in sorted(out)]


def okx_daily():
    out = {}
    after = ""
    while True:
        u = "https://www.okx.com/api/v5/market/history-candles?instId=BTC-USDT&bar=1Dutc&limit=100" + (f"&after={after}" if after else "")
        d = get(u)
        rows = d.get("data", [])
        if not rows:
            break
        for x in rows:
            if x[8] == "1":  # confirmed (closed) candles only
                out[int(x[0]) // 1000] = [int(x[0]) // 1000, float(x[1]), float(x[2]), float(x[3]), float(x[4])]
        after = rows[-1][0]
        if len(rows) < 100:
            break
        time.sleep(0.12)
    return [out[k] for k in sorted(out)]


def binance_1h():
    out = []
    start = 1502928000000
    hosts = ["https://data-api.binance.vision", "https://api.binance.com"]
    while True:
        d = None
        for h in hosts:
            try:
                d = get(f"{h}/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=1000&startTime={start}", tries=2)
                break
            except Exception as e:  # noqa: BLE001
                err = e
        if d is None:
            raise RuntimeError(err)
        if not d:
            break
        out += [[x[0] // 1000, float(x[1]), float(x[2]), float(x[3]), float(x[4])] for x in d]
        if len(d) < 1000:
            break
        start = d[-1][0] + 1
        time.sleep(0.1)
    return out


JOBS = {
    "yahoo_btc.json": lambda: yahoo("BTC-USD"),
    "yahoo_eth.json": lambda: yahoo("ETH-USD"),
    "bitstamp_btc.json": bitstamp,
    "okx_btc.json": okx_daily,
    "binance_1h.json": binance_1h,
}

if __name__ == "__main__":
    DATA.mkdir(exist_ok=True)
    force = "--force" in sys.argv
    for name, fn in JOBS.items():
        p = DATA / name
        if p.exists() and not force:
            print("cached", name)
            continue
        rows = fn()
        p.write_text(json.dumps(rows))
        print(name, len(rows), time.strftime("%Y-%m-%d %H:%M", time.gmtime(rows[0][0])), "->", time.strftime("%Y-%m-%d %H:%M", time.gmtime(rows[-1][0])))
