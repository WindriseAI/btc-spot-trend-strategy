"""The daily run: data -> signal -> account -> decision -> (at most one) order.

Modes
  report  read only. Computes the signal and what it *would* do. Never sends
          an order and never writes the state file.
  paper   OKX demo-trading keys; real orders on the demo exchange.
  live    real keys; needs LIVE_CONFIRM=yes.

Guarantees (each has a test):
  * at most one order per signal date, even if run many times or after a crash
  * the order is spot BTC-USDT, cash mode, IOC limit with a price cap
  * no order if price data is stale / missing / inconsistent across sources
  * no order with a key that can withdraw, with derivative positions open,
    or with any borrowed balance on the account
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from datetime import timedelta

from . import data as data_mod
from . import ledger
from .config import Config
from .exchange import ExchangeError, OkxSpot, OrderResult, SafetyError
from .state import StateError, Store, apply_flow, breaker_of, snapshot, upsert_history
from .strategy import (BreakerState, Decision, OrderPlan, Params, compute_signal, decide, drawdown, plan_order,
                       update_breaker)

MODES = ("report", "paper", "live")
STATE_NAME = {"live": "live", "demo": "paper"}
QUOTE_MAX_AGE_S = 120
MAX_SPREAD = 0.005


@dataclass
class RunResult:
    mode: str
    account: str = "none"                 # live | demo | none
    now: Optional[datetime] = None
    clock_skew_s: Optional[float] = None
    md: Optional[data_mod.MarketData] = None
    params: Params = field(default_factory=Params)
    # account
    have_account: bool = False
    first_run: bool = False
    btc: float = 0.0
    usdt: float = 0.0
    price: float = 0.0                    # mid price used for valuation
    equity: float = 0.0
    weight: float = 0.0
    weight_after: Optional[float] = None
    nav: float = 1.0
    peak_nav: float = 1.0
    dd: float = 0.0
    flow: float = 0.0
    net_deposits: float = 0.0
    breaker: Optional[BreakerState] = None
    breaker_event: str = ""
    scale: float = 1.0
    target: Optional[float] = None        # after the breaker scale
    decision: Optional[Decision] = None
    plan: Optional[OrderPlan] = None
    order: Optional[OrderResult] = None
    order_sent: bool = False
    already_traded: bool = False
    blocks: list = field(default_factory=list)     # reasons an intended trade was not sent
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    reconciled: Optional[str] = None
    todays_trade: Optional[dict] = None   # a rebalance already done for this signal date
    ledgers: Optional[dict] = None        # live vs the three shadow ledgers (see ledger.py)

    @property
    def needs_attention(self) -> bool:
        return bool(self.errors or self.blocks or self.breaker_event == "tripped"
                    or (self.md is not None and self.md.problems))


def cl_ord_id(signal_date, account: str) -> str:
    """Deterministic client order id: one per signal date and account."""
    return f"btcreb{signal_date:%Y%m%d}{'L' if account == 'live' else 'D'}"


def resolve_account(mode: str, cfg: Config, account: str = "auto") -> str:
    if mode == "live":
        return "live"
    if mode == "paper":
        return "demo"
    if account == "auto":
        return "live" if cfg.live_keys else ("demo" if cfg.demo_keys else "none")
    return account


def run_daily(
    mode: str,
    cfg: Config,
    account: str = "auto",
    now: Optional[datetime] = None,
    make_exchange: Optional[Callable] = None,
    get_data: Optional[Callable] = None,
    params: Params = Params(),
    sleep: Callable = time.sleep,
) -> RunResult:
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode}")
    r = RunResult(mode=mode, params=params)
    trading = mode in ("paper", "live")
    r.account = resolve_account(mode, cfg, account)

    # ---- clock: trust the exchange's clock over the local one --------------
    local = datetime.now(timezone.utc)
    if now is None:
        server = data_mod.okx_server_time(cfg.hostname)
        if server is not None:
            r.clock_skew_s = (local - server).total_seconds()
            if abs(r.clock_skew_s) > 300:
                r.warnings.append(f"本机时钟与交易所相差 {r.clock_skew_s:.0f} 秒，已改用交易所时间")
        now = server or local
    r.now = now

    # ---- hard pre-conditions ----------------------------------------------
    if mode == "live" and not cfg.live_confirm:
        r.errors.append("实盘模式需要环境变量 LIVE_CONFIRM=yes，未设置，已拒绝运行")
        return r
    keys = cfg.keys_for(r.account)
    if trading and keys is None:
        prefix = "OKX" if r.account == "live" else "OKX_DEMO"
        r.errors.append(f"没有找到 {prefix}_API_KEY / {prefix}_API_SECRET / {prefix}_API_PASSPHRASE，已拒绝运行")
        return r

    # ---- market data and signal -------------------------------------------
    get_data = get_data or data_mod.get_market_data
    try:
        md = get_data(now, params, cfg.max_source_diff, cfg.hostname)
    except Exception as e:  # noqa: BLE001
        md = data_mod.MarketData(signal_date=now.date() - timedelta(days=1))
        md.problems.append(f"行情数据获取出错：{type(e).__name__}: {str(e)[:200]}")
    r.md = md

    if r.account == "none" or keys is None:
        return r

    store = Store(cfg.state_dir, STATE_NAME[r.account])
    try:
        if trading:
            store.acquire()
        _account_part(r, cfg, store, keys, trading, make_exchange, sleep)
    except (StateError, ExchangeError) as e:
        r.errors.append(str(e))
    except SafetyError as e:
        r.errors.append(f"安全检查未通过，已停止：{e}")
    except Exception as e:  # noqa: BLE001  (never die without a report)
        r.errors.append(f"未预期的错误：{type(e).__name__}: {str(e)[:300]}")
    finally:
        store.release()
        if trading:
            store.log(_audit(r))
    return r


def _account_part(r: RunResult, cfg: Config, store: Store, keys, trading: bool, make_exchange, sleep) -> None:
    md, p, now = r.md, r.params, r.now
    today = now.date()
    state = store.load()
    r.first_run = not store.exists()
    ex = (make_exchange or _default_exchange)(keys, r.account == "demo", cfg)

    info = ex.key_info()
    if info.can_withdraw:
        raise SafetyError("这个 API key 带有【提现】权限。请到欧易删除它，重新创建只勾选【读取】和【交易】的 key")
    if trading and not info.can_trade:
        raise SafetyError("这个 API key 没有【交易】权限，无法下单")
    if r.account == "live" and not info.ip_bound:
        r.warnings.append("API key 没有绑定 IP：更不安全，而且连续 14 天不用会被欧易自动作废")

    inst = ex.instrument()
    q = ex.quote()
    bal = ex.balances()
    r.have_account = True

    # ---- finish anything a crashed run left behind ------------------------
    pend = state.get("pending_order")
    if pend:
        o = ex.get_order(pend["cl_ord_id"])
        _settle(state, pend, o, apply_to_snapshot=True)
        r.reconciled = (f"上次运行中断，已核对订单 {pend['cl_ord_id']}："
                        + (f"{_state_cn(o.state)}，成交 {o.filled:.8f} BTC" if o.found else "交易所查不到该订单（未成交）"))
        state["pending_order"] = None
        if trading:
            store.save(state)

    # ---- valuation, deposits, NAV, circuit breaker ------------------------
    mid = (q.bid + q.ask) / 2.0
    r.btc, r.usdt, r.price = bal.btc, bal.usdt, mid
    r.equity = bal.usdt + bal.btc * mid
    r.flow = apply_flow(state, bal.btc, bal.usdt, mid, today.isoformat())
    r.net_deposits = state.get("net_deposits", 0.0)
    if r.equity < 1.0 or state["units"] <= 0:
        r.nav = state.get("nav") or 1.0
        r.breaker = breaker_of(state)
        r.peak_nav = r.breaker.peak_nav
        r.dd = drawdown(r.nav, r.peak_nav)
        r.blocks.append("交易账户里几乎没有 BTC 或 USDT（不足 1 USDT）。请把资金从【资金账户】划转到【交易账户】")
        state["snapshot"] = snapshot(bal.btc, bal.usdt, mid, now.isoformat())
        if trading:
            store.save(state)
        return
    r.weight = bal.btc * mid / r.equity
    r.nav = r.equity / state["units"]
    br, r.scale, r.breaker_event = update_breaker(breaker_of(state), r.nav, today, p)
    r.breaker, r.peak_nav = br, br.peak_nav
    r.dd = drawdown(r.nav, br.peak_nav)
    state["breaker"], state["nav"] = br.to_dict(), r.nav

    # ---- shadow ledgers: same money, three other ways of running it --------
    sh = state.get("shadows")
    if sh is None:
        sh = state["shadows"] = ledger.init(bal.btc, bal.usdt, mid, cfg.default_fee, today)
    else:
        ledger.apply_flow(sh, r.flow, mid, cfg.default_fee)
    ledger.step(sh, today, mid, cfg.default_fee)
    if md.signal is not None:
        ledger.step_replay(sh, replay_rows(md, sh["replay"].get("last_date"), p), mid, r.scale, p.band, cfg.default_fee)
    r.ledgers = ledger.summary(sh, mid, r.equity)
    if abs(r.ledgers["replay_dev"]) > ledger.REPLAY_ALERT:
        r.warnings.append(f"实盘与「规则重放」相差 {r.ledgers['replay_dev']*100:+.1f}%（超过 2%）。"
                          "可能有漏跑、订单失败或成交偏差，请检查最近的日报")

    # ---- target and decision ----------------------------------------------
    if r.scale == 0.0:
        r.target = 0.0                       # breaker: sell to USDT whatever the signal says
    elif md.signal is not None:
        r.target = md.signal.T * r.scale
    if r.target is not None:
        dust_w = inst.min_size * mid / r.equity
        r.decision = decide(r.target, r.weight, p.band, dust_weight=dust_w)

    # ---- reasons not to trade ---------------------------------------------
    signal_date = md.signal_date.isoformat()
    r.already_traded = state.get("last_trade_signal_date") == signal_date
    if r.already_traded:
        done = [t for t in state.get("trades", []) if t.get("signal_date") == signal_date]
        r.todays_trade = done[-1] if done else None
    wants_trade = r.decision is not None and r.decision.action != "hold"
    positions = ex.open_derivative_positions()
    if positions and not (wants_trade and not r.already_traded):
        r.warnings.append("账户里有合约/杠杆持仓（" + "、".join(positions[:5]) + "）。本脚本不会碰它们；"
                          "在你手动平掉之前，脚本不会做现货买卖")
    if wants_trade:
        if r.already_traded:
            pass                              # not a problem: reported as "今日已调仓"
        else:
            if r.scale != 0.0 and not md.ok_to_trade:
                r.blocks.extend(md.problems or ["行情数据未通过检查"])
            if positions:
                r.blocks.append("账户里有合约/杠杆持仓（" + "、".join(positions[:5]) + "）。本脚本不会碰它们；"
                                "为避免影响保证金，在你手动平掉之前不做现货买卖")
            if bal.liabilities > 0:
                r.blocks.append("账户里有借币负债，本脚本只做无杠杆现货，已停止下单")
            if inst.state != "live":
                r.blocks.append(f"BTC-USDT 当前不可交易（状态 {inst.state}）")
            age = abs(now.timestamp() - q.ts_ms / 1000.0)
            if age > QUOTE_MAX_AGE_S:
                r.blocks.append(f"盘口报价已过期 {age:.0f} 秒")
            if not (q.bid > 0 and q.ask >= q.bid) or (q.ask / q.bid - 1) > MAX_SPREAD:
                r.blocks.append("盘口买卖价差异常，暂不下单")

    # ---- order -------------------------------------------------------------
    if wants_trade and not r.already_traded and not r.blocks:
        fee = ex.taker_fee(cfg.default_fee) if trading else cfg.default_fee
        r.plan = plan_order(
            target=r.target, btc=bal.btc, usdt=bal.usdt, bid=q.bid, ask=q.ask,
            lot=inst.lot, min_size=inst.min_size, tick=inst.tick,
            slippage_cap=cfg.slippage_cap, fee_rate=fee,
            min_notional=cfg.min_order_usdt, max_notional=cfg.max_order_usdt,
            btc_avail=bal.btc_avail, usdt_avail=bal.usdt_avail,
        )
        if r.plan.side in ("buy", "sell") and trading:
            cid = cl_ord_id(md.signal_date, r.account)
            prior = ex.get_order(cid)
            if prior.found:
                # the state file was lost but today's order already exists on the exchange
                r.already_traded = True
                r.order = prior
                state["last_trade_signal_date"] = signal_date
                r.warnings.append("交易所上已有今天的调仓订单（状态文件可能丢失过），不再重复下单")
            else:
                intent = {"cl_ord_id": cid, "signal_date": signal_date, "side": r.plan.side,
                          "size": r.plan.size_btc, "limit_price": r.plan.limit_price, "at": now.isoformat()}
                # write-ahead: mark the day as used BEFORE sending, so no crash can cause a second order
                state["pending_order"] = intent
                state["last_trade_signal_date"] = signal_date
                state["snapshot"] = snapshot(bal.btc, bal.usdt, mid, now.isoformat())
                store.save(state)
                ex.place_ioc(r.plan.side, r.plan.size_btc, r.plan.limit_price, cid, inst)
                r.order_sent = True
                o = OrderResult(False)
                for i in range(6):
                    o = ex.get_order(cid)
                    if o.found and o.state in ("filled", "canceled", "mmp_canceled"):
                        break
                    sleep(0.5 * (i + 1))
                r.order = o
                _settle(state, intent, o, apply_to_snapshot=False)
                state["pending_order"] = None
                if not o.found or o.state not in ("filled", "canceled", "mmp_canceled"):
                    r.warnings.append("订单已发送，但未能确认最终状态，请到欧易 App 的订单记录里核对")
                elif o.filled <= 0:
                    r.warnings.append("订单没有成交（价格在瞬间超出了滑点上限），今天不再重试，明天会重新判断")
                bal = ex.balances()
                q2 = ex.quote()
                mid = (q2.bid + q2.ask) / 2.0
                eq2 = bal.usdt + bal.btc * mid
                r.weight_after = bal.btc * mid / eq2 if eq2 > 0 else 0.0

    # ---- persist -----------------------------------------------------------
    state["snapshot"] = snapshot(bal.btc, bal.usdt, mid, now.isoformat())
    sig = md.signal
    upsert_history(state, {
        "date": signal_date,
        "close": sig.close if sig else None, "m": sig.m if sig else None, "vol": sig.vol if sig else None,
        "V": sig.V if sig else None, "T": sig.T if sig else None, "scale": r.scale, "target": r.target,
        "weight": r.weight, "weight_after": r.weight_after, "equity": r.equity, "nav": r.nav,
        "dd": r.dd, "flow": r.flow, "action": _action_code(r), "breaker": br.status,
        "led": {k: round(v, 6) for k, v in (r.ledgers or {}).items()},
    })
    state["last_run"] = {"at": now.isoformat(), "signal_date": signal_date, "mode": r.mode, "action": _action_code(r)}
    if trading:
        store.save(state)


def replay_rows(md, last_date: Optional[str], p: Params) -> list:
    """(date, close, raw target, raw target under the old rule) for every signal
    date after `last_date` that the available closes can reproduce; only the
    current date on a first run."""
    closes = md.closes_long or md.closes or []
    n = p.min_history
    rows = []
    for back in range(len(closes) - n, -1, -1):
        d = md.signal_date - timedelta(days=back)
        if (last_date is None and back > 0) or (last_date is not None and d.isoformat() <= last_date):
            continue
        end = len(closes) - back
        sig = md.signal if back == 0 else compute_signal(closes[end - n:end], p)
        rows.append((d, closes[end - 1], sig.T, sig.T if sig.T_plain is None else sig.T_plain))
    return rows


def _default_exchange(keys, demo: bool, cfg: Config) -> OkxSpot:
    return OkxSpot(keys, demo=demo, hostname=cfg.hostname, proxy=cfg.proxy, builder_code=cfg.builder_code)


def _settle(state: dict, intent: dict, o: OrderResult, apply_to_snapshot: bool) -> None:
    """Record an order's outcome; optionally roll its fill into the snapshot
    (needed when the run that sent it died before taking a new snapshot)."""
    state.setdefault("trades", []).append({
        "signal_date": intent["signal_date"], "cl_ord_id": intent["cl_ord_id"], "side": intent["side"],
        "size": intent["size"], "limit_price": intent["limit_price"], "found": o.found, "state": o.state,
        "filled": o.filled, "avg_price": o.avg_price, "fee": o.fee, "fee_ccy": o.fee_ccy, "ord_id": o.ord_id,
    })
    snap = state.get("snapshot")
    if not (apply_to_snapshot and snap and o.found and o.filled > 0):
        return
    fee_btc = o.fee if o.fee_ccy == "BTC" else 0.0
    fee_usdt = o.fee if o.fee_ccy == "USDT" else 0.0
    if intent["side"] == "buy":
        snap["btc"] += o.filled - fee_btc
        snap["usdt"] -= o.filled * o.avg_price + fee_usdt
    else:
        snap["btc"] -= o.filled + fee_btc
        snap["usdt"] += o.filled * o.avg_price - fee_usdt


def _action_code(r: RunResult) -> str:
    if r.order_sent and r.order and r.order.filled > 0:
        return r.plan.side
    if r.order_sent:
        return "sent_unfilled"
    if r.blocks:
        return "blocked"
    if r.already_traded:
        return "already_traded"
    if r.plan is not None and r.plan.side in ("buy", "sell"):
        return f"would_{r.plan.side}"
    return "hold"


def _state_cn(s: str) -> str:
    return {"filled": "完全成交", "partially_filled": "部分成交", "canceled": "已撤销（未成交部分）",
            "live": "挂单中", "mmp_canceled": "已撤销"}.get(s, s or "未知")


def _audit(r: RunResult) -> dict:
    sig = r.md.signal if r.md else None
    return {
        "at": r.now.isoformat() if r.now else None, "mode": r.mode, "account": r.account,
        "signal_date": r.md.signal_date.isoformat() if r.md else None,
        "close": sig.close if sig else None, "m": sig.m if sig else None, "T": sig.T if sig else None,
        "target": r.target, "weight": r.weight, "equity": r.equity, "nav": r.nav, "dd": r.dd,
        "action": _action_code(r), "plan": vars(r.plan) if r.plan else None,
        "order": vars(r.order) if r.order else None, "blocks": r.blocks, "warnings": r.warnings,
        "errors": r.errors, "breaker": r.breaker.to_dict() if r.breaker else None, "flow": r.flow,
    }
