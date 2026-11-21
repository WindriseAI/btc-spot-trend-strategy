"""Account-level backtest engine.

Simulates a real cash account (USDT + BTC quantities) rather than multiplying
weights by returns, and takes every decision through the same functions the
live script uses (btc.strategy.compute_signal / decide / update_breaker), so
weight drift, the rebalance band, fees, slippage and the circuit breaker
behave exactly as they would live.

Timeline for day t (a UTC day whose close is at 00:00 UTC of the next day):
  1. equity is marked at close[t]
  2. the signal is computed from closes up to and including close[t]
  3. the order (if any) is filled at fill[t], the first price we can really get
     after the signal: close[t] itself (idealised, delay 0), the price h hours
     later, or close[t+1] (24 h late).
"""
from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from btc.strategy import BreakerState, Params, compute_signal, decide, update_breaker  # noqa: E402

DATA = ROOT / "data"


# ---------------------------------------------------------------- data ----

@dataclass
class Daily:
    days: list            # list[date]; days[i] is the UTC day whose close is close[i]
    open: list
    high: list
    low: list
    close: list

    def idx(self, d) -> int:
        d = date.fromisoformat(d) if isinstance(d, str) else d
        return self.days.index(d)

    def __len__(self):
        return len(self.days)


def _load(name: str):
    return json.loads((DATA / name).read_text())


def load_daily(name: str, drop_last_before: Optional[date] = None) -> Daily:
    """Load [ts, o, h, l, c] rows keyed by the bar's opening UTC timestamp.

    Rows for a day that is not finished yet are dropped (a daily bar is only
    usable once its UTC day is over)."""
    rows = _load(name)
    today = datetime.now(timezone.utc).date() if drop_last_before is None else drop_last_before
    out = {}
    for ts, o, h, l, c in rows:
        d = datetime.fromtimestamp(ts, timezone.utc).date()
        if d >= today or c is None:
            continue
        out[d] = (o, h, l, c)
    days = sorted(out)
    return Daily(days, [out[d][0] for d in days], [out[d][1] for d in days], [out[d][2] for d in days], [out[d][3] for d in days])


def gaps(d: Daily) -> list:
    return [(a, b) for a, b in zip(d.days, d.days[1:]) if (b - a).days != 1]


def align(a: Daily, b: Daily) -> tuple[Daily, Daily]:
    """Restrict both series to their common days."""
    common = sorted(set(a.days) & set(b.days))
    ia = {d: i for i, d in enumerate(a.days)}
    ib = {d: i for i, d in enumerate(b.days)}

    def pick(s, ix):
        return Daily(common, [s.open[ix[d]] for d in common], [s.high[ix[d]] for d in common],
                     [s.low[ix[d]] for d in common], [s.close[ix[d]] for d in common])
    return pick(a, ia), pick(b, ib)


def patch_missing(a: Daily, b: Daily) -> tuple[Daily, list]:
    """Fill days missing from `a` (inside or after its range, up to b's end) with b's bars."""
    have = set(a.days)
    ib = {d: i for i, d in enumerate(b.days)}
    rows = {d: (a.open[i], a.high[i], a.low[i], a.close[i]) for i, d in enumerate(a.days)}
    added = []
    for d in b.days:
        if d > a.days[0] and d not in have:
            i = ib[d]
            rows[d] = (b.open[i], b.high[i], b.low[i], b.close[i])
            added.append(d)
    days = sorted(rows)
    return Daily(days, [rows[d][0] for d in days], [rows[d][1] for d in days], [rows[d][2] for d in days], [rows[d][3] for d in days]), added


def splice(early: Daily, late: Daily) -> Daily:
    """Use `late` from its first day on and `early` before it (for lookback history)."""
    cut = late.days[0]
    k = [i for i, d in enumerate(early.days) if d < cut]
    return Daily([early.days[i] for i in k] + late.days, [early.open[i] for i in k] + late.open,
                 [early.high[i] for i in k] + late.high, [early.low[i] for i in k] + late.low,
                 [early.close[i] for i in k] + late.close)


def load_hourly(name: str = "binance_1h.json") -> dict:
    """Map bar-open unix ts -> (open, close)."""
    return {r[0]: (r[1], r[4]) for r in _load(name)}


def hourly_fill_series(base: Daily, hourly: dict, delay_h: int) -> tuple[list, list]:
    """Daily closes and fills taken from hourly bars.

    Returns (close, fill): close[i] is the hourly-bar price at the end of UTC
    day days[i] (falls back to `base` where the hourly data does not exist yet,
    so the lookback history is complete), fill[i] is the price `delay_h` hours
    after that close (None when unavailable).
    """
    close, fill = [], []
    for i, d in enumerate(base.days):
        t_close = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()) + 86400
        bar = hourly.get(t_close)             # bar that opens exactly at the daily close
        close.append(bar[0] if bar else base.close[i])
        fb = hourly.get(t_close + delay_h * 3600)
        fill.append(fb[0] if (fb and bar) else None)
    return close, fill


# ----------------------------------------------------------- simulation ----

@dataclass
class Result:
    days: list
    equity: list          # marked at each close, starts at 1.0
    weight: list          # BTC weight after that day's trade
    target: list
    trades: list          # (day, side, weight_before, weight_after, notional_share)
    fees: float
    breaker_events: list
    low_equity: list      # equity marked at the day's low (intraday worst case)
    paid: list = None     # cumulative money paid in (initial + deposits), per day


def targets(closes: Sequence[float], p: Params) -> list:
    """Raw target T for every day (None until there is enough history)."""
    n = p.min_history
    out: list = [None] * len(closes)
    for t in range(n - 1, len(closes)):
        out[t] = compute_signal(closes[t - n + 1:t + 1], p).T
    return out


def targets2(closes: Sequence[float], p: Params) -> tuple[list, list]:
    """(targets under p, targets under the original rule without the down-penalty) in one pass."""
    n = p.min_history
    new: list = [None] * len(closes)
    old: list = [None] * len(closes)
    for t in range(n - 1, len(closes)):
        s = compute_signal(closes[t - n + 1:t + 1], p)
        new[t], old[t] = s.T, s.T_plain
    return new, old


def simulate(
    days: Sequence[date],
    close: Sequence[float],
    fill: Sequence[Optional[float]],
    start,
    end,
    p: Params = Params(),
    fee: float = 0.001,
    slippage: float = 0.0005,
    tgt: Optional[Sequence[Optional[float]]] = None,
    low: Optional[Sequence[float]] = None,
    min_trade_share: float = 0.0,
    initial: float = 1.0,
    deposit_every: int = 0,
    deposit_amount: float = 0.0,
) -> Result:
    """Run the rule from the close of `start` to the close of `end`.

    `tgt` lets a caller supply a different target series (benchmarks); it is
    still passed through the band/exit logic unless p.band == 0.
    `fee` is charged on traded notional, `slippage` worsens every fill price.
    """
    start = date.fromisoformat(start) if isinstance(start, str) else start
    end = date.fromisoformat(end) if isinstance(end, str) else end
    i0, i1 = list(days).index(start), list(days).index(end)
    T = tgt if tgt is not None else targets(close, p)
    usdt, btc, paid = initial, 0.0, initial
    br = BreakerState(peak_nav=initial)
    res = Result([], [], [], [], [], 0.0, [], [], [])
    for t in range(i0, i1 + 1):
        if deposit_every and t > i0 and (t - i0) % deposit_every == 0:
            usdt += deposit_amount          # new savings simply join the equity (rule 5)
            paid += deposit_amount
        res.paid.append(paid)
        eq = usdt + btc * close[t]
        res.days.append(days[t])
        res.equity.append(eq)
        res.low_equity.append(usdt + btc * (low[t] if low else close[t]))
        raw = T[t]
        px = fill[t] if t < len(fill) else None
        if raw is None:
            res.weight.append(btc * close[t] / eq)
            res.target.append(None)
            continue
        br, scale, event = update_breaker(br, eq, days[t], p)
        if event:
            res.breaker_events.append((days[t], event, eq))
        target = raw * scale
        res.target.append(target)
        if px is None or t == i1:            # nothing fillable / last day: no trade
            res.weight.append(btc * close[t] / eq)
            continue
        eq_px = usdt + btc * px
        w = btc * px / eq_px
        d = decide(target, w, p.band)
        if d.action == "buy":
            notional = min((target - w) * eq_px, usdt / (1 + fee))
            if notional > min_trade_share * eq_px:
                btc += notional / (px * (1 + slippage))
                usdt -= notional * (1 + fee)
                res.fees += notional * fee
                res.trades.append((days[t], "buy", w, target, notional / eq_px))
        elif d.action == "sell":
            qty = btc if target <= 0 else min(btc, (w - target) * eq_px / px)
            notional = qty * px * (1 - slippage)
            if notional > min_trade_share * eq_px:
                btc -= qty
                usdt += notional * (1 - fee)
                res.fees += notional * fee
                res.trades.append((days[t], "sell", w, target, notional / eq_px))
        e2 = usdt + btc * px
        res.weight.append(btc * px / e2 if e2 > 0 else 0.0)
    return res


def simulate_static(days, close, start, end, weight=1.0, fee=0.001, slippage=0.0005, rebalance_band=None,
                    rebalance_every=None, initial=1.0, deposit_every=0, deposit_amount=0.0) -> Result:
    """Buy & hold (weight=1), or a constant mix rebalanced on drift / every N days.
    With deposits and weight=1 this is "deposit, buy, hold" (DCA)."""
    start = date.fromisoformat(start) if isinstance(start, str) else start
    end = date.fromisoformat(end) if isinstance(end, str) else end
    i0, i1 = list(days).index(start), list(days).index(end)
    usdt, btc, paid = initial, 0.0, initial
    res = Result([], [], [], [], [], 0.0, [], [], [])
    for t in range(i0, i1 + 1):
        px = close[t]
        dep = bool(deposit_every and t > i0 and (t - i0) % deposit_every == 0)
        if dep:
            usdt += deposit_amount
            paid += deposit_amount
        res.paid.append(paid)
        eq = usdt + btc * px
        res.days.append(days[t]); res.equity.append(eq); res.low_equity.append(eq)
        w = btc * px / eq
        if (t == i0 or (rebalance_band is not None and abs(w - weight) >= rebalance_band)
                or (rebalance_every and (t - i0) % rebalance_every == 0) or (dep and weight >= 1.0)):
            diff = (weight - w) * eq
            if diff > 0:
                n = min(diff, usdt / (1 + fee)); btc += n / (px * (1 + slippage)); usdt -= n * (1 + fee)
            else:
                q = -diff / px; btc -= q; usdt += q * px * (1 - slippage) * (1 - fee)
            res.trades.append((days[t], "rebal", w, weight, abs(diff) / eq))
        res.weight.append(btc * px / (usdt + btc * px)); res.target.append(weight)
    return res


# ---------------------------------------------------------------- stats ----

def stats(r: Result) -> dict:
    eq = r.equity
    if r.paid and r.paid[-1] != r.paid[0]:
        raise ValueError("stats() is for runs without deposits; use plan_stats()")
    n = len(eq) - 1
    yrs = n / 365.0
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq))]
    cagr = (eq[-1] / eq[0]) ** (1 / yrs) - 1 if yrs > 0 and eq[-1] > 0 else float("nan")
    pk, dd, dd_low = eq[0], 0.0, 0.0
    for e, lo in zip(eq, r.low_equity):
        dd_low = min(dd_low, lo / pk - 1)
        pk = max(pk, e)
        dd = min(dd, e / pk - 1)
    m = sum(rets) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in rets) / (n - 1))
    downside = math.sqrt(sum(min(x, 0) ** 2 for x in rets) / n)
    turnover = sum(t[4] for t in r.trades) / yrs
    return dict(
        total=eq[-1] / eq[0], cagr=cagr, maxdd=-dd, maxdd_low=-dd_low,
        vol=sd * math.sqrt(365), sharpe=(m / sd * math.sqrt(365)) if sd > 0 else 0.0,
        sortino=(m / downside * math.sqrt(365)) if downside > 0 else 0.0,
        mar=(cagr / -dd) if dd < 0 else float("nan"),
        trades=len(r.trades), trades_per_year=len(r.trades) / yrs, turnover=turnover,
        exposure=sum(r.weight) / len(r.weight), fees=r.fees, years=yrs,
        in_market=sum(1 for w in r.weight if w > 0.01) / len(r.weight),
    )


def plan_stats(r: Result) -> dict:
    """For runs with deposits: money-weighted outcome versus what was paid in."""
    rel = [e / p for e, p in zip(r.equity, r.paid)]
    return dict(final_over_paid=rel[-1], worst_interim=min(rel) - 1.0, paid=r.paid[-1], final=r.equity[-1])


def yearly(r: Result) -> dict:
    out = {}
    for y in sorted({d.year for d in r.days}):
        ix = [i for i, d in enumerate(r.days) if d.year == y]
        a = ix[0] - 1 if ix[0] > 0 else ix[0]
        out[y] = r.equity[ix[-1]] / r.equity[a] - 1
    return out


def fmt(s: dict) -> str:
    return (f"CAGR {s['cagr']*100:6.1f}%  maxDD {s['maxdd']*100:5.1f}%  Sharpe {s['sharpe']:.2f}  "
            f"vol {s['vol']*100:4.0f}%  total {s['total']:7.2f}x  trades/yr {s['trades_per_year']:5.1f}  "
            f"exposure {s['exposure']:.2f}")


def shift_fill(close: Sequence[float], lag_days: int) -> list:
    """fill[t] = close[t+lag_days] (None past the end)."""
    n = len(close)
    return [close[t + lag_days] if t + lag_days < n else None for t in range(n)]
