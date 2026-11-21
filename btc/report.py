"""Plain-Chinese text reports (daily and weekly)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from .engine import RunResult, _state_cn
from .strategy import Params

BJ = timezone(timedelta(hours=8))
MODE_CN = {"report": "只看信号（不下单）", "paper": "模拟盘", "live": "实盘"}
ACCOUNT_CN = {"live": "实盘账户", "demo": "模拟盘账户", "none": "未连接账户"}


def pct(x: float, digits: int = 1) -> str:
    return f"{x * 100:.{digits}f}%"


def money(x: float) -> str:
    return f"{x:,.2f}"


def _days(xs) -> str:
    return "、".join(str(x) for x in xs) + " 日" if xs else "无"


def breaker_line(r: RunResult) -> str:
    p = r.params
    trig = pct(p.breaker_drawdown or 0, 0)
    b = r.breaker
    if b is None:
        return f"熔断：未启用账户跟踪（回撤达 {trig} 触发）"
    if r.breaker_event == "tripped":
        return (f"熔断：⚠️ 今天触发！账户从最高点回撤已达 {trig}。全部卖成 USDT，暂停 {p.breaker_pause_days} 天"
                f"（到 {b.resume_on}），之后以半仓恢复，需要你本人确认才回到正常仓位")
    if b.status == "paused":
        return f"熔断：⚠️ 暂停中（{b.tripped_on} 触发），{b.resume_on} 之后以半仓恢复"
    if b.status == "half":
        return "熔断：⚠️ 暂停期已过，目前按【半仓】运行；等你本人确认，或半仓满 12 个月后，才恢复正常仓位"
    if r.breaker_event == "auto_resumed":
        return "熔断：半仓已满 12 个月且没有再次触发，今天起自动恢复正常仓位"
    return f"熔断：正常（回撤达 {trig} 才触发，现在 {pct(r.dd)}）"


def action_line(r: RunResult) -> str:
    d, plan, o = r.decision, r.plan, r.order
    band = pct(r.params.band, 0)
    if r.blocks:
        return "操作：⚠️ 没有下单 —— " + "；".join(r.blocks)
    if d is None:
        return "操作：无法判断（没有可用的信号）"
    if r.order_sent and o is not None:
        side = "买入" if plan.side == "buy" else "卖出"
        if o.found and o.filled > 0:
            after = f"，仓位变为 {pct(r.weight_after)}" if r.weight_after is not None else ""
            return (f"操作：已{side} {o.filled:.8f} BTC，均价 {money(o.avg_price)}，约 {money(o.filled * o.avg_price)} USDT"
                    f"（{_state_cn(o.state)}）{after}")
        return f"操作：已发出{side}订单但没有成交，今天不再重试"
    if r.already_traded and d.action != "hold":
        return "操作：今天已经调过一次仓，不再重复下单"
    if d.action == "hold":
        if r.todays_trade and r.todays_trade.get("filled", 0) > 0:
            t = r.todays_trade
            side = "买入" if t["side"] == "buy" else "卖出"
            return f"操作：今天已完成调仓（{side} {t['filled']:.8f} BTC，均价 {money(t['avg_price'])}），无需再操作"
        if d.reason == "already_flat":
            return "操作：无需操作 —— 目标仓位是 0，现在已经空仓（全部是 USDT）"
        return (f"操作：无需操作 —— 目标 {pct(d.target, 0)} 与当前 {pct(d.weight, 0)} 只差 {pct(abs(d.delta), 0)}，"
                f"不到 {band} 的调仓门槛")
    if plan is not None and plan.side == "none":
        return "操作：无需操作 —— 需要调整的数量低于交易所最小下单量"
    side = "买入" if d.action == "buy" else "卖出"
    why = "目标仓位为 0，全部卖出" if d.reason == "exit" else f"目标 {pct(d.target, 0)} 与当前 {pct(d.weight, 0)} 相差达到 {band}"
    if plan is not None:
        return (f"操作：【只看信号，未下单】按规则应{side}约 {plan.size_btc:.8f} BTC（约 {money(plan.notional)} USDT）—— {why}")
    return f"操作：按规则应{side} —— {why}"


def daily_report(r: RunResult) -> str:
    now_bj = (r.now or datetime.now(timezone.utc)).astimezone(BJ)
    md = r.md
    L = []
    flag = "⚠️ " if r.needs_attention else ""
    L.append(f"{flag}【BTC 策略日报】{now_bj:%Y-%m-%d}（北京时间 {now_bj:%H:%M}）｜{MODE_CN.get(r.mode, r.mode)}")
    sig = md.signal if md else None
    if sig is not None:
        L.append(f"BTC 收盘价（UTC {md.signal_date:%m-%d}）：{money(sig.close)}")
        L.append(f"趋势：{sig.m}/{sig.n} 个回看期上涨（上涨：{_days(sig.up)}；下跌：{_days(sig.down)}）→ E = {sig.E:.2f}")
        L.append(f"波动率 vol30：{pct(sig.vol)} → V = {sig.V:.2f}")
        L.append(f"目标仓位 T = E×V：{pct(sig.T, 0)}")
    elif md is not None:
        L.append("行情：⚠️ 今天没有拿到可用的日线数据，无法计算信号")
    if md is None:
        L.append("状态：⚠️ 没有运行（原因见下面的错误）")
    elif r.have_account:
        if r.scale != 1.0 and sig is not None and r.target is not None:
            L.append(f"熔断调整后的实际目标仓位：{pct(r.target, 0)}")
        L.append(f"当前仓位：{pct(r.weight, 0)}（BTC {r.btc:.8f}，USDT {money(r.usdt)}）")
        L.append(action_line(r))
        L.append(f"账户净值：{money(r.equity)} USDT｜距最高点回撤：{pct(r.dd)}")
        if abs(r.flow) > 0:
            L.append(f"检测到{'入金' if r.flow > 0 else '出金'}：{money(abs(r.flow))} USDT（已计入本金，不算盈亏）")
        L.append(breaker_line(r))
        if r.first_run:
            L.append("提示：这是本账户第一次运行，已新建状态文件（如果不是第一次，说明状态文件丢了，请立即检查）")
    elif r.account == "none":
        L.append("当前仓位：未连接账户（没有配置 API key），只显示信号")
        L.append(f"熔断：未启用账户跟踪（回撤达 {pct(r.params.breaker_drawdown or 0, 0)} 触发）")
    else:
        L.append(f"账户：⚠️ 没能读取{ACCOUNT_CN.get(r.account, r.account)}")
    if md is not None and md.primary:
        src = f"数据：{md.primary}"
        if md.checker:
            src += f"，已用 {md.checker} 交叉核对（最新收盘相差 {pct(md.diff_last or 0, 2)}）"
        L.append(src)
    if r.reconciled:
        L.append("补充：" + r.reconciled)
    notes = list(r.warnings) + (list(md.notes) if md else [])
    for w in notes:
        L.append("提醒：" + w)
    errs = list(r.errors)
    if md is not None and not r.blocks:
        errs += [x for x in md.problems if x not in errs]
    L.append("错误：" + ("；".join(errs) if errs else "无"))
    return "\n".join(L)


EXPECTATION = ("合理预期：这套规则的目标是「少亏」，不是「多赚」。长期年化大约 10–20%，任意 3 年亏钱的可能约 1/4 到 1/3，"
               "账面回撤可能到 -45% ~ -50%，也可能连续两三年不创新高。")


def _vs(v: float, base: float) -> str:
    return f"{(v / base - 1) * 100:+.1f}%" if base > 0 else "—"


def ledgers_block(led: dict, prev: Optional[dict] = None) -> list:
    """Three ledgers side by side: same starting money, same deposits."""
    paid = led.get("paid_in") or 0.0
    same_money = prev is not None and abs((prev.get("paid_in") or 0.0) - paid) < 1e-6

    def line(label: str, key: str) -> str:
        v = led.get(key) or 0.0
        txt = f"  {label}：{money(v)} USDT（相对投入 {_vs(v, paid)}"
        if same_money and prev.get(key):
            txt += f"，本周 {_vs(v, prev[key])}"
        return txt + "）"
    out = [f"四本账对比（同样的本金和存款，累计投入 {money(paid)} USDT）：",
           line("① 实盘（新权重）", "live"),
           line("② 影子：旧权重（上涨数/7，不扣下跌项）", "old"),
           line("③ 影子：存入就买、一直持有", "dca"),
           line("④ 影子：一半 BTC 一半 USDT，每月调平", "half")]
    dev = led.get("replay_dev") or 0.0
    flag = "⚠️ 超过 2%，可能是漏跑、订单失败或成交偏差，请检查" if abs(dev) > 0.02 else "正常（2% 以内）"
    out.append(f"  实盘与「规则重放」的偏差：{dev * 100:+.1f}% —— {flag}")
    return out


def weekly_report(rows: list, state: Optional[dict], account: str, now: datetime, p: Params = Params(), source: str = "") -> str:
    """rows: [(day, close, Signal)] oldest first (normally 7 days + the day before)."""
    now_bj = now.astimezone(BJ)
    L = [f"【BTC 策略周报】截至 {now_bj:%Y-%m-%d}（北京时间）"]
    if len(rows) >= 2:
        first, last = rows[0], rows[-1]
        L.append(f"BTC 一周涨跌：{pct(last[1] / first[1] - 1)}（{money(first[1])} → {money(last[1])}）")
        L.append(f"目标仓位变化：{pct(first[2].T, 0)} → {pct(last[2].T, 0)}")
    L.append("日期(UTC)｜收盘价｜趋势｜波动率｜目标仓位")
    for day, close, sig in rows[1:] if len(rows) > 1 else rows:
        L.append(f"{day:%m-%d}｜{money(close)}｜{sig.m}/{sig.n} 上涨｜{pct(sig.vol, 0)}｜{pct(sig.T, 0)}")
    if state and state.get("history"):
        days = {d.isoformat() for d, _, _ in rows}
        h = [x for x in state["history"] if x.get("date") in days]
        L.append(f"—— {ACCOUNT_CN.get(account, account)} ——")
        if h:
            a, b = h[0], h[-1]
            if a.get("nav") and b.get("nav"):
                L.append(f"单位净值：{a['nav']:.4f} → {b['nav']:.4f}（{pct(b['nav'] / a['nav'] - 1)}，已剔除出入金）")
            L.append(f"账户总值：{money(b.get('equity') or 0)} USDT｜当前仓位：{pct(b.get('weight_after') if b.get('weight_after') is not None else b.get('weight') or 0, 0)}｜距最高点回撤：{pct(b.get('dd') or 0)}")
        trades = [t for t in state.get("trades", []) if t.get("signal_date") in days and t.get("filled", 0) > 0]
        if trades:
            L.append(f"本周调仓 {len(trades)} 次：")
            for t in trades:
                L.append(f"  {t['signal_date'][5:]} {'买入' if t['side'] == 'buy' else '卖出'} {t['filled']:.8f} BTC，均价 {money(t['avg_price'])}")
        else:
            L.append("本周调仓 0 次")
        flows = [f for f in state.get("flows", []) if f.get("date", "") >= min(days)]
        if flows:
            L.append("本周出入金：" + "；".join(f"{f['date'][5:]} {'+' if f['usdt'] > 0 else ''}{money(f['usdt'])} USDT" for f in flows))
        led_rows = [x for x in h if x.get("led")]
        if led_rows:
            L.extend(ledgers_block(led_rows[-1]["led"], led_rows[0]["led"] if len(led_rows) > 1 else None))
        st = (state.get("breaker") or {}).get("status", "normal")
        L.append("熔断状态：" + {"normal": "正常", "paused": "⚠️ 暂停中",
                               "half": "⚠️ 半仓运行，等待用户确认（或满 12 个月）"}.get(st, st))
    else:
        L.append("账户：没有运行记录（只显示信号）")
    if source:
        L.append(f"数据：{source}")
    L.append(EXPECTATION)
    return "\n".join(L)
