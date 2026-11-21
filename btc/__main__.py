"""Command line entry point.

  python -m btc report            today's signal + what it would do (no orders)
  python -m btc paper             trade on OKX demo trading
  python -m btc live              trade for real (needs LIVE_CONFIRM=yes)
  python -m btc weekly            weekly summary
  python -m btc confirm-resume paper|live   owner re-confirms after a circuit breaker

Exit code 0 = fine (including "nothing to do"), 1 = something needs attention.
The report is always printed to stdout as plain text.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from . import data as data_mod
from .config import ConfigError, load_config
from .engine import STATE_NAME, resolve_account, run_daily
from .report import daily_report, weekly_report
from .state import StateError, Store, breaker_of
from .strategy import Params, confirm_resume


def _utf8() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass


def cmd_weekly(cfg, account: str) -> int:
    p = Params()
    now = data_mod.okx_server_time(cfg.hostname) or datetime.now(timezone.utc)
    last_day = now.date() - timedelta(days=1)
    need = p.min_history + 7
    closes, source, errs = None, "", []
    for name, fn, _ in data_mod.SOURCES:
        try:
            series = {d: v for d, v in fn(cfg.hostname).items() if d <= last_day}
            closes, source = data_mod.window(series, last_day, need), name
            break
        except Exception as e:  # noqa: BLE001
            errs.append(f"{name}: {str(e)[:120]}")
    if closes is None:
        print("⚠️【BTC 策略周报】没有拿到可用的行情数据：" + "；".join(errs))
        return 1
    rows = data_mod.recent_signals(closes, last_day, 8, p)
    acct = resolve_account("report", cfg, account)
    state = None
    if acct != "none" or account in ("live", "demo"):
        name = STATE_NAME.get(acct if acct != "none" else account)
        store = Store(cfg.state_dir, name)
        if store.exists():
            try:
                state = store.load()
            except StateError as e:
                print(f"提醒：{e}")
    else:
        for a in ("live", "demo"):               # no keys here: still show whichever record exists
            store = Store(cfg.state_dir, STATE_NAME[a])
            if store.exists():
                try:
                    state, acct = store.load(), a
                    break
                except StateError:
                    pass
    print(weekly_report(rows, state, acct, now, p, source))
    return 0


def cmd_confirm(cfg, which: str) -> int:
    store = Store(cfg.state_dir, which)
    try:
        store.acquire()
        if not store.exists():
            print(f"没有找到 {which} 的状态文件，无需确认")
            return 1
        s = store.load()
        b = breaker_of(s)
        if b.status != "half":
            print({"normal": "熔断状态本来就是正常，无需确认",
                   "paused": f"还在暂停期内（到 {b.resume_on}），暂停期结束后才能确认恢复"}.get(b.status, b.status))
            return 1
        s["breaker"] = confirm_resume(b).to_dict()
        store.save(s)
        store.log({"at": datetime.now(timezone.utc).isoformat(), "event": "confirm_resume"})
        print("已确认：从下一次运行起恢复正常仓位（不再减半）")
        return 0
    except StateError as e:
        print(f"⚠️ {e}")
        return 1
    finally:
        store.release()


def main(argv=None) -> int:
    _utf8()
    ap = argparse.ArgumentParser(prog="python -m btc", description="BTC 现货趋势策略（欧易 OKX，只做多、不加杠杆）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, helptext in (("report", "只看信号，不下单"), ("weekly", "周报")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--account", choices=("auto", "live", "demo", "none"), default="auto",
                        help="读取哪个账户的仓位来显示（只读）。默认：有实盘 key 用实盘，否则模拟盘，否则不连账户")
    sub.add_parser("paper", help="模拟盘：用欧易模拟交易的 key 真实下单（模拟资金）")
    sub.add_parser("live", help="实盘：需要 LIVE_CONFIRM=yes")
    sp = sub.add_parser("confirm-resume", help="熔断暂停期结束后，由用户确认恢复正常仓位")
    sp.add_argument("which", choices=("paper", "live"))
    a = ap.parse_args(argv)
    try:
        cfg = load_config()
    except (ConfigError, ValueError) as e:
        print(f"⚠️【BTC 策略】配置有误，未运行：{e}")
        return 1
    if a.cmd == "weekly":
        return cmd_weekly(cfg, a.account)
    if a.cmd == "confirm-resume":
        return cmd_confirm(cfg, a.which)
    r = run_daily(a.cmd, cfg, account=getattr(a, "account", "auto"))
    print(daily_report(r))
    return 1 if r.needs_attention else 0


if __name__ == "__main__":
    sys.exit(main())
