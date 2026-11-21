"""Re-run the independent review's own code (reference/review-code) on fresh data.

The review's proposal (ensemble of 7 lookbacks x 40% vol target, 0.20 band) was
"P4b" in its exp4.py. This script rebuilds the three input files that code
expects and prints the same statistics, so its headline numbers can be checked.

Usage:  python backtest/reproduce_review.py
"""
from __future__ import annotations

import contextlib
import io
import json
import math
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
END = "2026-11-20"
END_TS = int(datetime(2026, 11, 20, tzinfo=timezone.utc).timestamp())


def _kraken() -> dict:
    cache = DATA / "kraken_daily.json"
    if cache.exists():
        return json.loads(cache.read_text())
    with urllib.request.urlopen("https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=1440", timeout=30) as r:
        d = json.load(r)
    cache.write_text(json.dumps(d))
    return d


def reproduce() -> dict:
    """Returns {(start, lag, fee): (cagr, maxdd)} from the review's code."""
    tmp = Path(tempfile.mkdtemp(prefix="review_"))
    rows = [r for r in json.loads((DATA / "yahoo_btc.json").read_text()) if r[0] < END_TS]   # as the review had it
    chart = {"chart": {"result": [{"timestamp": [r[0] for r in rows], "indicators": {"quote": [{
        "open": [r[1] for r in rows], "high": [r[2] for r in rows], "low": [r[3] for r in rows], "close": [r[4] for r in rows]}]}}]}}
    (tmp / "yahoo.json").write_text(json.dumps(chart))
    (tmp / "kraken.json").write_text(json.dumps(_kraken()))       # the review took the last day from Kraken
    (tmp / "funding.json").write_text("[]")                        # perp funding: not used by the spot rule
    sys.path.insert(0, str(ROOT / "reference" / "review-code"))
    argv, sys.argv = sys.argv, ["bt.py", str(tmp)]
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            import bt  # noqa: PLC0415  (the review's module; reads sys.argv[1] at import)
    finally:
        sys.argv = argv
    C, N, ret = bt.C, bt.N, bt.ret

    def vol(t, n=30):
        x = ret[t - n + 1:t + 1]
        m = sum(x) / n
        return (sum((a - m) ** 2 for a in x) / (n - 1)) ** 0.5 * math.sqrt(365)

    K = (20, 40, 60, 90, 120, 180, 240)
    ens = [0.0] * N
    for t in range(max(K), N):
        ens[t] = sum(1 for k in K if C[t] > C[t - k]) / len(K)
    vt = [0.0] * N
    for t in range(60, N):
        vt[t] = ens[t] * min(1.0, 0.40 / max(vol(t), 1e-9))
    sig, cur = [0.0] * N, 0.0
    for t in range(N):
        if abs(vt[t] - cur) >= 0.2 or (vt[t] == 0 and cur != 0):
            cur = vt[t]
        sig[t] = cur
    out = {}
    for s0 in ("2018-01-01", "2022-01-01"):
        for lag in (0, 1):
            for fee in (0.0005, 0.001):
                eq, rs, _ = bt.run(sig, s0, END, lag, fee)
                st = bt.stats(eq, rs)
                out[(s0, lag, fee)] = (st["cagr"], -st["dd"])
    return out


if __name__ == "__main__":
    for (s0, lag, fee), (cagr, dd) in reproduce().items():
        print(f"review code  {s0}..{END}  lag={lag}  fee={fee:.4f}  CAGR {cagr*100:5.1f}%  maxDD {dd*100:5.1f}%")
