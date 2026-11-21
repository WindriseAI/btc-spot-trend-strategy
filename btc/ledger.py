"""Shadow ledgers kept next to the real account from its first run.

They receive exactly the same starting money and the same deposits and
withdrawals as the real account, so their values can be compared directly:

  dca     "deposit and hold": every deposit is turned into BTC and held.
  half    50% BTC / 50% USDT, rebalanced on the first run of each month.
  old     the original rule (E = up/7, no penalty for "down" lookbacks),
          replayed on paper, to compare the new weight with the old one.
  replay  the live rule itself, replayed on paper from daily closes. If the
          real account drifts more than 2% away from it, something is wrong
          with execution or the program (missed days, failed orders, bad fills).

Pure bookkeeping: no network, no orders.
"""
from __future__ import annotations

from datetime import date
from typing import Optional, Sequence

from .strategy import decide

REPLAY_ALERT = 0.02
NAMES = ("dca", "half", "old", "replay")


def value(l: dict, price: float) -> float:
    return l["usdt"] + l["btc"] * price


def _buy(l: dict, usdt_amount: float, price: float, fee: float) -> None:
    usdt_amount = min(usdt_amount, l["usdt"])
    if usdt_amount > 0 and price > 0:
        l["usdt"] -= usdt_amount
        l["btc"] += usdt_amount * (1 - fee) / price


def _sell(l: dict, btc_amount: float, price: float, fee: float) -> None:
    btc_amount = min(btc_amount, l["btc"])
    if btc_amount > 0:
        l["btc"] -= btc_amount
        l["usdt"] += btc_amount * price * (1 - fee)


def _to_weight(l: dict, w: float, price: float, fee: float) -> None:
    eq = value(l, price)
    diff = w * eq - l["btc"] * price
    if diff > 0:
        _buy(l, diff, price, fee)
    elif diff < 0:
        _sell(l, -diff / price, price, fee)


def init(btc: float, usdt: float, price: float, fee: float, today: date) -> dict:
    """Start all three ledgers with the account's value on its first run."""
    eq = usdt + btc * price
    dca = {"btc": 0.0, "usdt": eq}
    _buy(dca, eq, price, fee)
    half = {"btc": 0.0, "usdt": eq, "month": today.strftime("%Y-%m")}
    _to_weight(half, 0.5, price, fee)
    replay = {"btc": btc, "usdt": usdt, "last_date": None}     # starts as the real account is
    old = {"btc": btc, "usdt": usdt, "last_date": None}
    return {"dca": dca, "half": half, "old": old, "replay": replay, "paid_in": eq, "started": today.isoformat()}


def apply_flow(sh: dict, flow: float, price: float, fee: float) -> None:
    """Give every ledger the same deposit (flow > 0) or withdrawal (flow < 0)."""
    if flow == 0:
        return
    sh["paid_in"] = sh.get("paid_in", 0.0) + flow
    if "old" not in sh:
        sh["old"] = dict(sh["replay"])
    for name in NAMES:
        l = sh[name]
        if flow > 0:
            l["usdt"] += flow
        else:
            need = -flow - l["usdt"]
            if need > 0:                                   # not enough cash: sell BTC to pay it out
                _sell(l, need / (price * (1 - fee)) if price > 0 else 0.0, price, fee)
            l["usdt"] = max(0.0, l["usdt"] + flow)
    if flow > 0:
        _buy(sh["dca"], flow, price, fee)                  # deposit and hold
        _buy(sh["half"], flow / 2.0, price, fee)           # half of every deposit into BTC


def step(sh: dict, today: date, price: float, fee: float) -> None:
    """Monthly rebalance of the 50/50 ledger (first run of a new month)."""
    month = today.strftime("%Y-%m")
    if sh["half"].get("month") != month:
        _to_weight(sh["half"], 0.5, price, fee)
        sh["half"]["month"] = month


def step_replay(sh: dict, rows: Sequence, price_now: float, scale: float, band: float, fee: float) -> None:
    """Advance the two rule replays through every signal date not yet processed.

    rows: [(date, close, raw_target, raw_target_old_rule)] oldest first, ending
    with the current signal date. Days the script missed are replayed at that
    day's close; the current day at the price the real order would get now.
    """
    if "old" not in sh:                                        # state written before this ledger existed
        sh["old"] = dict(sh["replay"])
    _step_rule(sh["replay"], [(r[0], r[1], r[2]) for r in rows], price_now, scale, band, fee)
    _step_rule(sh["old"], [(r[0], r[1], r[3] if len(r) > 3 else r[2]) for r in rows], price_now, scale, band, fee)


def _step_rule(l: dict, rows: Sequence, price_now: float, scale: float, band: float, fee: float) -> None:
    last: Optional[str] = l.get("last_date")
    for i, (d, close, raw_t) in enumerate(rows):
        if last is not None and d.isoformat() <= last:
            continue
        px = price_now if i == len(rows) - 1 else close
        eq = value(l, px)
        if eq <= 0:
            l["last_date"] = d.isoformat()
            continue
        target = raw_t * scale
        dec = decide(target, l["btc"] * px / eq, band)
        if dec.action != "hold":
            _to_weight(l, target, px, fee)
        l["last_date"] = d.isoformat()


def summary(sh: dict, price: float, live_equity: float) -> dict:
    paid = sh.get("paid_in", 0.0)
    out = {"paid_in": paid, "live": live_equity}
    for name in NAMES:
        out[name] = value(sh[name], price) if name in sh else live_equity
    rep = out["replay"]
    out["replay_dev"] = (live_equity / rep - 1.0) if rep > 0 else 0.0
    return out
