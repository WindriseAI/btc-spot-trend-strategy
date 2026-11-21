"""Persistent state: one JSON file per account ("paper" or "live").

Written atomically (temp file + rename) with a .bak of the previous version,
so a crash can never leave a half-written file. A lock file stops two runs
from overlapping.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from .strategy import BreakerState

VERSION = 1
HISTORY_KEEP = 800
FLOWS_KEEP = 200
LOCK_STALE_SECONDS = 15 * 60


class StateError(Exception):
    pass


def new_state(account: str) -> dict:
    return {
        "version": VERSION,
        "account": account,
        "created_at": None,
        "units": 0.0,                 # fund-style units; NAV = equity / units
        "nav": 1.0,
        "breaker": BreakerState().to_dict(),
        "snapshot": None,             # {"btc","usdt","price","equity","at"} after the last run
        "net_deposits": 0.0,          # sum of detected deposits minus withdrawals, USDT
        "flows": [],                  # [{"date","usdt"}]
        "last_trade_signal_date": None,
        "pending_order": None,        # write-ahead intent: set before an order is sent
        "last_run": None,
        "shadows": None,              # comparison ledgers, see ledger.py
        "history": [],                # one row per signal date
        "trades": [],
    }


class Store:
    def __init__(self, state_dir: Path, account: str):
        self.dir = Path(state_dir)
        self.account = account
        self.path = self.dir / f"{account}.json"
        self.bak = self.dir / f"{account}.json.bak"
        self.lock_path = self.dir / f"{account}.lock"
        self.log_path = self.dir / f"{account}-log.jsonl"
        self._locked = False

    # ---- load / save ----------------------------------------------------
    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> dict:
        """Load the state. A missing file means a first run; a corrupt file is
        an error (we must not silently forget the circuit breaker)."""
        if not self.path.is_file():
            return new_state(self.account)
        try:
            s = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise StateError(f"状态文件损坏，无法读取：{self.path}（可用同目录的 .bak 文件恢复）：{e}") from e
        if not isinstance(s, dict) or s.get("version") != VERSION or s.get("account") != self.account:
            raise StateError(f"状态文件格式不对或不属于本账户：{self.path}")
        base = new_state(self.account)
        base.update(s)
        return base

    def save(self, s: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        s["history"] = s.get("history", [])[-HISTORY_KEEP:]
        s["flows"] = s.get("flows", [])[-FLOWS_KEEP:]
        s["trades"] = s.get("trades", [])[-HISTORY_KEEP:]
        tmp = self.path.with_suffix(".json.tmp")
        data = json.dumps(s, ensure_ascii=False, indent=1, sort_keys=True)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if self.path.is_file():
            try:
                os.replace(self.path, self.bak)
            except OSError:
                pass
        os.replace(tmp, self.path)

    def log(self, record: dict) -> None:
        """Append-only audit trail (never contains secrets)."""
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n")
        except OSError:
            pass

    # ---- lock -----------------------------------------------------------
    def acquire(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()} {time.time():.0f}".encode())
                os.close(fd)
                self._locked = True
                return
            except FileExistsError:
                try:
                    age = time.time() - self.lock_path.stat().st_mtime
                except OSError:
                    age = 0
                if age > LOCK_STALE_SECONDS:
                    try:
                        os.remove(self.lock_path)
                    except OSError:
                        pass
                    continue
                raise StateError("另一个实例正在运行（锁文件存在），本次不执行")
        raise StateError("无法获取锁文件")

    def release(self) -> None:
        if self._locked:
            try:
                os.remove(self.lock_path)
            except OSError:
                pass
            self._locked = False


def breaker_of(s: dict) -> BreakerState:
    return BreakerState.from_dict(s.get("breaker") or {})


def upsert_history(s: dict, row: dict) -> None:
    h = s.setdefault("history", [])
    if h and h[-1].get("date") == row.get("date"):
        done = ("buy", "sell", "sent_unfilled")
        if h[-1].get("action") in done and row.get("action") not in done:
            # a later run on the same day must not hide the trade made earlier
            row = dict(row, action=h[-1]["action"], weight=h[-1].get("weight"),
                       weight_after=h[-1].get("weight_after"))
        h[-1] = row
    else:
        h.append(row)


def snapshot(btc: float, usdt: float, price: float, at: str) -> dict:
    return {"btc": btc, "usdt": usdt, "price": price, "equity": usdt + btc * price, "at": at}


def apply_flow(s: dict, btc: float, usdt: float, price: float, day: str, min_abs: float = 0.5, min_rel: float = 0.002) -> float:
    """Detect deposits/withdrawals since the last snapshot and adjust units.

    Between runs the script places no orders, so any change in the BTC or USDT
    *quantities* came from outside (deposit, withdrawal, transfer, manual
    trade). Its value is added to/removed from the unit count at the current
    NAV, so the NAV — and therefore the drawdown — is unaffected.
    Returns the detected flow in USDT (0 if none).
    """
    equity = usdt + btc * price
    snap: Optional[dict] = s.get("snapshot")
    if not snap or s.get("units", 0) <= 0:
        # first run: start the unit count so that NAV continues from the stored value
        nav = s.get("nav") or 1.0
        s["units"] = equity / nav if equity > 0 else 0.0
        if s.get("created_at") is None:
            s["created_at"] = day
        return 0.0
    flow = (usdt - snap["usdt"]) + (btc - snap["btc"]) * price
    if abs(flow) < max(min_abs, min_rel * max(equity, 0.0)):
        return 0.0
    pre_equity = equity - flow
    nav_pre = pre_equity / s["units"] if s["units"] > 0 else 0.0
    if pre_equity <= 1e-6 or nav_pre <= 1e-9:
        # the account was empty before this deposit: keep the last NAV
        nav = s.get("nav") or 1.0
        s["units"] = equity / nav if equity > 0 else 0.0
    else:
        s["units"] = max(0.0, s["units"] + flow / nav_pre)
    s["net_deposits"] = s.get("net_deposits", 0.0) + flow
    s.setdefault("flows", []).append({"date": day, "usdt": round(flow, 4)})
    return flow
