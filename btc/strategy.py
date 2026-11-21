"""Pure strategy logic: signal maths, rebalance decision, circuit breaker.

No I/O, no network, no clock. Everything here is shared by the live script
and the backtest so that the two cannot drift apart.

The rule (see README):
  up/down = how many of the lookbacks {20,40,60,90,120,180,240} have close > / <= close N days ago
  E = max(0, up - 0.3 * down) / 7      (owner's decision 2026-11-21; the original rule was E = up / 7)
  V = min(1, 0.40 / vol30)   vol30 = stdev of last 30 daily log returns * sqrt(365)
  T = E * V                  target share of equity held in spot BTC (0..1)
  Rebalance only when |T - weight| >= 0.20, or when T == 0 and BTC is still held.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from typing import Optional, Sequence

LOOKBACKS: tuple[int, ...] = (20, 40, 60, 90, 120, 180, 240)
VOL_WINDOW = 30
VOL_TARGET = 0.40
DOWN_PENALTY = 0.3   # each "down" lookback cancels 0.3 "up" lookbacks (0 = the original plain count)
BAND = 0.20
DAYS_PER_YEAR = 365  # crypto trades every day

BREAKER_DRAWDOWN = 0.45
BREAKER_PAUSE_DAYS = 30
BREAKER_RESUME_SCALE = 0.5
BREAKER_HALF_MAX_DAYS = 365   # half size ends when the owner confirms, or after 12 months


@dataclass(frozen=True)
class Params:
    lookbacks: tuple[int, ...] = LOOKBACKS
    vol_window: int = VOL_WINDOW
    vol_target: Optional[float] = VOL_TARGET  # None = no volatility scaling
    band: float = BAND
    down_penalty: float = DOWN_PENALTY
    breaker_drawdown: Optional[float] = BREAKER_DRAWDOWN  # None = breaker off
    breaker_pause_days: int = BREAKER_PAUSE_DAYS
    breaker_resume_scale: float = BREAKER_RESUME_SCALE
    breaker_half_max_days: int = BREAKER_HALF_MAX_DAYS

    @property
    def min_history(self) -> int:
        """Number of daily closes needed to compute a signal."""
        return max(max(self.lookbacks), self.vol_window) + 1


@dataclass(frozen=True)
class Signal:
    close: float
    up: tuple[int, ...]      # lookbacks with close > close N days ago
    down: tuple[int, ...]
    m: int
    n: int
    E: float
    vol: float               # annualised
    V: float
    T: float                 # raw target weight E*V (before circuit breaker)
    E_plain: Optional[float] = None   # m/n, the original rule without the down-penalty
    T_plain: Optional[float] = None   # E_plain*V, kept for the "old rule" shadow ledger


def realized_vol(closes: Sequence[float], window: int = VOL_WINDOW) -> float:
    """Annualised sample stdev of the last `window` daily log returns."""
    if len(closes) < window + 1:
        raise ValueError(f"need {window + 1} closes for vol, got {len(closes)}")
    c = closes[-(window + 1):]
    r = [math.log(c[i] / c[i - 1]) for i in range(1, len(c))]
    mean = sum(r) / window
    var = sum((x - mean) ** 2 for x in r) / (window - 1)
    return math.sqrt(var) * math.sqrt(DAYS_PER_YEAR)


def compute_signal(closes: Sequence[float], p: Params = Params()) -> Signal:
    """Signal from completed daily closes; closes[-1] is the latest close."""
    if len(closes) < p.min_history:
        raise ValueError(f"need {p.min_history} closes, got {len(closes)}")
    for x in closes[-p.min_history:]:
        if not isinstance(x, (int, float)) or x != x or x <= 0:
            raise ValueError("closes contain non-positive or NaN values")
    last = closes[-1]
    up = tuple(k for k in p.lookbacks if last > closes[-1 - k])
    down = tuple(k for k in p.lookbacks if k not in up)
    n = len(p.lookbacks)
    E_plain = len(up) / n
    E = max(0.0, len(up) - p.down_penalty * len(down)) / n
    vol = realized_vol(closes, p.vol_window)
    if p.vol_target is None or vol <= 0:
        V = 1.0
    else:
        V = min(1.0, p.vol_target / vol)
    return Signal(close=last, up=up, down=down, m=len(up), n=n, E=E, vol=vol, V=V, T=E * V,
                  E_plain=E_plain, T_plain=E_plain * V)


# --------------------------------------------------------------------------
# Rebalance decision
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Decision:
    action: str          # "buy" | "sell" | "hold"
    reason: str          # band | exit | within_band | already_flat
    target: float
    weight: float
    delta: float         # target - weight


EPS = 1e-9


def decide(target: float, weight: float, band: float = BAND, dust_weight: float = 0.0) -> Decision:
    """Compare the target weight with the current BTC weight.

    * target == 0 and BTC held  -> sell everything (no band).
    * |target - weight| >= band -> trade to target.
    * otherwise hold.
    `dust_weight`: a BTC weight at or below this counts as "no BTC" (unsellable dust).
    """
    target = min(1.0, max(0.0, target))
    weight = max(0.0, weight)
    delta = target - weight
    if target <= EPS:
        if weight > dust_weight + EPS:
            return Decision("sell", "exit", 0.0, weight, delta)
        return Decision("hold", "already_flat", 0.0, weight, delta)
    if abs(delta) >= band - EPS:
        return Decision("buy" if delta > 0 else "sell", "band", target, weight, delta)
    return Decision("hold", "within_band", target, weight, delta)


# --------------------------------------------------------------------------
# Order sizing
# --------------------------------------------------------------------------

def _decimals(step: float) -> int:
    return max(0, -int(math.floor(math.log10(step) + 1e-12)))


def floor_to_step(x: float, step: float) -> float:
    """Round x down to a multiple of step without float noise (e.g. lot size 1e-8)."""
    if step <= 0:
        return x
    n = math.floor(x / step + 1e-9)
    return round(n * step, _decimals(step) + 2)


def ceil_to_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    n = math.ceil(x / step - 1e-9)
    return round(n * step, _decimals(step) + 2)


@dataclass(frozen=True)
class OrderPlan:
    side: str            # "buy" | "sell" | "none"
    size_btc: float      # base-currency size, rounded down to the lot size
    limit_price: float   # worst acceptable price (slippage cap), on the tick grid
    notional: float      # size * reference price, USDT
    skip_reason: str = ""


def plan_order(
    *,
    target: float,
    btc: float,
    usdt: float,
    bid: float,
    ask: float,
    lot: float,
    min_size: float,
    tick: float,
    slippage_cap: float,
    fee_rate: float,
    min_notional: float = 0.0,
    max_notional: Optional[float] = None,
    btc_avail: Optional[float] = None,
    usdt_avail: Optional[float] = None,
) -> OrderPlan:
    """Size one spot order that moves the BTC weight to `target`.

    Buys are sized so that cost at the worst-case (cap) price plus fee never
    exceeds the USDT actually available: borrowing is impossible by construction.
    Sells never exceed the BTC held: shorting is impossible by construction.
    """
    if not (bid > 0 and ask > 0 and ask >= bid):
        return OrderPlan("none", 0.0, 0.0, 0.0, "bad_quote")
    btc = max(0.0, btc)
    usdt = max(0.0, usdt)
    btc_free = btc if btc_avail is None else max(0.0, min(btc, btc_avail))      # not frozen
    usdt_free = usdt if usdt_avail is None else max(0.0, min(usdt, usdt_avail))
    mid = (bid + ask) / 2.0
    equity = usdt + btc * mid
    if equity <= 0:
        return OrderPlan("none", 0.0, 0.0, 0.0, "no_equity")
    target = min(1.0, max(0.0, target))
    diff_value = target * equity - btc * mid
    if diff_value > 0:
        limit = ceil_to_step(ask * (1 + slippage_cap), tick)
        affordable = usdt_free / (1 + fee_rate) / limit        # BTC we can pay for at the worst price
        size = min(diff_value / ask, affordable)
        if max_notional is not None:
            size = min(size, max_notional / ask)
        size = floor_to_step(size, lot)
        side, ref = "buy", ask
    else:
        limit = floor_to_step(bid * (1 - slippage_cap), tick)
        size = btc_free if target <= EPS else min(btc_free, -diff_value / bid)
        if max_notional is not None:
            size = min(size, max_notional / bid)
        size = floor_to_step(size, lot)
        side, ref = "sell", bid
    notional = size * ref
    if size <= 0 or size < min_size - 1e-15:
        return OrderPlan("none", size, limit, notional, "below_min_size")
    if notional < min_notional:
        return OrderPlan("none", size, limit, notional, "below_min_notional")
    return OrderPlan(side, size, limit, notional)


# --------------------------------------------------------------------------
# Account circuit breaker. NAV is unitised (deposits/withdrawals buy/redeem
# units), so adding savings never looks like a gain and never hides a loss.
# --------------------------------------------------------------------------

@dataclass
class BreakerState:
    status: str = "normal"            # normal | paused | half
    peak_nav: float = 1.0
    tripped_on: Optional[str] = None  # ISO date of the last trip
    resume_on: Optional[str] = None   # ISO date when the pause ends
    trip_nav: Optional[float] = None
    half_since: Optional[str] = None  # ISO date the half-size period started

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "BreakerState":
        return cls(
            status=d.get("status", "normal"),
            peak_nav=float(d.get("peak_nav", 1.0)),
            tripped_on=d.get("tripped_on"),
            resume_on=d.get("resume_on"),
            trip_nav=d.get("trip_nav"),
            half_since=d.get("half_since"),
        )


def update_breaker(b: BreakerState, nav: float, today: date, p: Params = Params()) -> tuple[BreakerState, float, str]:
    """Advance the breaker with today's NAV.

    Drawdown is measured on the unit NAV (deposits and withdrawals excluded).
    Returns (new_state, scale, event). `scale` multiplies the raw target:
    1.0 normal, 0.0 while paused, 0.5 after the pause until the owner confirms
    or 12 months have passed. event is "", "tripped", "pause_ended" or
    "auto_resumed".
    """
    nb = BreakerState(b.status, b.peak_nav, b.tripped_on, b.resume_on, b.trip_nav, b.half_since)
    event = ""
    if p.breaker_drawdown is None:
        nb.peak_nav = max(nb.peak_nav, nav)
        return nb, 1.0, event
    if nb.status == "paused":
        if nb.resume_on and today >= date.fromisoformat(nb.resume_on):
            nb.status = "half"
            nb.peak_nav = nav           # drawdown is measured afresh from the restart
            nb.resume_on = None
            nb.half_since = today.isoformat()
            event = "pause_ended"
        else:
            return nb, 0.0, event
    elif nb.status == "half" and nb.half_since:
        if today >= date.fromisoformat(nb.half_since) + timedelta(days=p.breaker_half_max_days):
            nb.status = "normal"        # 12 months at half size without a new trip: back to full
            nb.half_since = None
            event = "auto_resumed"
    nb.peak_nav = max(nb.peak_nav, nav)
    dd = drawdown(nav, nb.peak_nav)
    if dd >= p.breaker_drawdown - EPS:
        nb.status = "paused"
        nb.tripped_on = today.isoformat()
        nb.resume_on = (today + timedelta(days=p.breaker_pause_days)).isoformat()
        nb.trip_nav = nav
        nb.half_since = None
        return nb, 0.0, "tripped"
    return nb, (p.breaker_resume_scale if nb.status == "half" else 1.0), event


def confirm_resume(b: BreakerState) -> BreakerState:
    """Owner re-confirms after a breaker pause: back to full size."""
    if b.status != "half":
        raise ValueError(f"nothing to confirm: breaker status is '{b.status}'")
    return BreakerState("normal", b.peak_nav, b.tripped_on, None, b.trip_nav, None)


def drawdown(nav: float, peak: float) -> float:
    return 0.0 if peak <= 0 else max(0.0, 1.0 - nav / peak)
