"""Price-data validation, state persistence, config, and backtest/live consistency."""
import json
import os
from datetime import date, datetime, timedelta, timezone

import pytest

from btc import data as D
from btc.config import ConfigError, Keys, load_config, load_dotenv
from btc.report import weekly_report
from btc.state import StateError, Store, apply_flow, new_state, snapshot
from btc.strategy import Params, compute_signal

NOW = datetime(2026, 11, 21, 0, 22, tzinfo=timezone.utc)
LAST = date(2026, 11, 20)


def make_series(last=LAST, n=300, start=100.0, step=0.5, extra_today=True, scale=1.0):
    s = {last - timedelta(days=i): (start + (n - 1 - i) * step) * scale for i in range(n)}
    if extra_today:
        s[last + timedelta(days=1)] = 1.0          # the unfinished bar of today: must be ignored
    return s


def src(name, series, quote="USDT"):
    def fn(_host):
        if isinstance(series, Exception):
            raise series
        return dict(series)
    return (name, fn, quote)


# ------------------------------------------------------------------ data ----

def test_two_agreeing_sources_allow_trading_and_todays_bar_is_ignored():
    md = D.get_market_data(NOW, sources=[src("A", make_series()), src("B", make_series(scale=1.001))])
    assert md.ok_to_trade and md.primary == "A" and md.checker == "B"
    assert md.signal_date == LAST and md.closes[-1] == pytest.approx(100.0 + 299 * 0.5)
    assert md.signal.m == 7 and md.diff_last == pytest.approx(0.001, rel=1e-3)


def test_stale_primary_falls_back_and_all_stale_refuses():
    stale = make_series(last=LAST - timedelta(days=1), extra_today=False)
    md = D.get_market_data(NOW, sources=[src("A", stale), src("B", make_series()), src("C", make_series())])
    assert md.ok_to_trade and md.primary == "B" and "A" in md.source_errors and "过期" in md.source_errors["A"]
    md2 = D.get_market_data(NOW, sources=[src("A", stale), src("B", stale)])
    assert not md2.ok_to_trade and md2.signal is None and md2.problems


def test_single_source_reports_but_does_not_trade():
    md = D.get_market_data(NOW, sources=[src("A", make_series()), src("B", OSError("down"))])
    assert md.signal is not None and not md.ok_to_trade and "交叉验证" in md.problems[0]


def test_gap_or_bad_value_rejects_a_source():
    gap = make_series()
    del gap[LAST - timedelta(days=100)]
    bad = make_series()
    bad[LAST - timedelta(days=3)] = 0.0
    jump = make_series()
    jump[LAST - timedelta(days=3)] *= 3
    md = D.get_market_data(NOW, sources=[src("gap", gap), src("bad", bad), src("jump", jump), src("ok", make_series())])
    assert set(md.source_errors) == {"gap", "bad", "jump"} and md.primary == "ok" and not md.ok_to_trade


def test_disagreeing_sources_refuse_to_trade():
    md = D.get_market_data(NOW, sources=[src("A", make_series()), src("B", make_series(scale=1.03))], max_diff=0.015)
    assert not md.ok_to_trade and "相差" in md.problems[0]
    down = make_series(step=-0.1)                        # same last price region, opposite trend
    md2 = D.get_market_data(NOW, sources=[src("A", make_series(start=100, step=0.0001)),
                                          src("B", {k: 100.0299 + (v - 100.0299) * -1 for k, v in make_series(start=100, step=0.0001).items()})])
    assert not md2.ok_to_trade and any("目标仓位不一致" in p for p in md2.problems)
    assert down


def test_window_returns_oldest_first_and_exact_length():
    w = D.window(make_series(), LAST, 241)
    assert len(w) == 241 and w[-1] > w[0]
    with pytest.raises(D.DataError):
        D.window({}, LAST, 241)


def test_recent_signals_and_weekly_report_text():
    closes = D.window(make_series(n=260), LAST, 249)
    rows = D.recent_signals(closes, LAST, 8)
    assert [r[0] for r in rows][-1] == LAST and len(rows) == 8
    assert rows[-1][2] == compute_signal(closes[-241:])
    st = new_state("paper")
    st["history"] = [{"date": (LAST - timedelta(days=6)).isoformat(), "nav": 1.0, "equity": 100, "weight": 0.5, "dd": 0.0},
                     {"date": LAST.isoformat(), "nav": 1.05, "equity": 105, "weight": 0.5, "dd": 0.01}]
    st["trades"] = [{"signal_date": LAST.isoformat(), "side": "buy", "filled": 0.001, "avg_price": 60000.0}]
    txt = weekly_report(rows, st, "demo", NOW, Params(), "OKX")
    for needle in ("BTC 策略周报", "BTC 一周涨跌", "目标仓位", "单位净值：1.0000 → 1.0500", "本周调仓 1 次", "熔断状态：正常"):
        assert needle in txt, needle
    assert "没有运行记录" in weekly_report(rows, None, "none", NOW)


# ----------------------------------------------------------------- state ----

def test_state_roundtrip_backup_and_corruption(tmp_path):
    s = Store(tmp_path, "paper")
    st = s.load()
    assert st == new_state("paper") and not s.exists()
    st["nav"] = 1.23
    s.save(st)
    st["nav"] = 1.5
    s.save(st)
    assert s.load()["nav"] == 1.5
    assert json.loads(s.bak.read_text(encoding="utf-8"))["nav"] == 1.23     # previous version kept
    assert not list(tmp_path.glob("*.tmp"))
    s.path.write_text('{"version": 1, "account": "live"}', encoding="utf-8")
    with pytest.raises(StateError):
        s.load()                                                            # another account's file
    s.path.write_text("garbage", encoding="utf-8")
    with pytest.raises(StateError):
        s.load()


def test_lock_blocks_second_instance_and_stale_lock_is_cleared(tmp_path):
    a, b = Store(tmp_path, "paper"), Store(tmp_path, "paper")
    a.acquire()
    with pytest.raises(StateError):
        b.acquire()
    old = a.lock_path.stat().st_mtime - 3600
    os.utime(a.lock_path, (old, old))
    b.acquire()                                                             # stale lock (crashed run) is replaced
    b.release()
    assert not a.lock_path.exists()


def test_unitised_nav_ignores_deposits_and_withdrawals():
    st = new_state("paper")
    assert apply_flow(st, 0.0, 100.0, 60000.0, "2026-01-01") == 0.0
    assert st["units"] == pytest.approx(100.0)
    st["snapshot"] = snapshot(0.001, 40.0, 60000.0, "t")                    # after buying 0.001 BTC
    # next day: price +10%, and the owner deposits 50 USDT
    flow = apply_flow(st, 0.001, 90.0, 66000.0, "2026-01-02")
    assert flow == pytest.approx(50.0)
    equity = 90.0 + 0.001 * 66000.0
    assert equity / st["units"] == pytest.approx(1.06)                      # +6% from price only
    st["snapshot"] = snapshot(0.001, 90.0, 66000.0, "t")
    assert apply_flow(st, 0.001, 90.0, 50000.0, "2026-01-03") == 0.0        # pure price move is not a flow
    assert apply_flow(st, 0.001, 90.2, 50000.0, "2026-01-03") == 0.0        # dust is ignored
    flow = apply_flow(st, 0.0005, 60.0, 50000.0, "2026-01-04")              # withdraw BTC and USDT
    assert flow == pytest.approx(-55.0)
    assert st["net_deposits"] == pytest.approx(-5.0)
    assert (60.0 + 25.0) / st["units"] == pytest.approx((90.0 + 50.0) / (156.0 / 1.06))


def test_account_emptied_then_refunded_keeps_nav():
    st = new_state("paper")
    apply_flow(st, 0.0, 100.0, 60000.0, "d1")
    st["nav"] = 0.8
    st["snapshot"] = snapshot(0.0, 80.0, 60000.0, "t")
    apply_flow(st, 0.0, 0.0, 60000.0, "d2")
    assert st["units"] == pytest.approx(0.0)
    st["snapshot"] = snapshot(0.0, 0.0, 60000.0, "t")
    apply_flow(st, 0.0, 200.0, 60000.0, "d3")
    assert 200.0 / st["units"] == pytest.approx(0.8)                         # drawdown history survives


# ---------------------------------------------------------------- config ----

def test_dotenv_and_config(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith("OKX") or k in ("LIVE_CONFIRM", "STATE_DIR", "SLIPPAGE_CAP", "MAX_ORDER_USDT"):
            monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text('# comment\nOKX_DEMO_API_KEY=k1\nOKX_DEMO_API_SECRET="s1"\nOKX_DEMO_API_PASSPHRASE=p1\nLIVE_CONFIRM=YES\n'
                   f"STATE_DIR={tmp_path / 'st'}\n", encoding="utf-8")
    monkeypatch.setenv("BTC_ENV_FILE", str(env))
    cfg = load_config()
    assert cfg.demo_keys.secret == "s1" and cfg.live_keys is None and cfg.live_confirm
    assert cfg.state_dir == tmp_path / "st" and cfg.slippage_cap == 0.003
    assert cfg.builder_code is None
    assert "s1" not in repr(cfg) and "k1" not in repr(cfg) and repr(Keys("a", "b", "c")) == "Keys(***)"
    monkeypatch.setenv("OKX_AI_BUILDER_CODE", "  MyCode123  ")
    assert load_config().builder_code == "MyCode123"
    monkeypatch.setenv("OKX_AI_BUILDER_CODE", "   ")
    assert load_config().builder_code is None
    monkeypatch.delenv("OKX_AI_BUILDER_CODE")
    monkeypatch.setenv("LIVE_CONFIRM", "no")                                # real env wins over .env
    assert not load_config().live_confirm
    monkeypatch.setenv("OKX_API_KEY", "only-one")
    with pytest.raises(ConfigError):
        load_config()
    monkeypatch.delenv("OKX_API_KEY")
    monkeypatch.setenv("SLIPPAGE_CAP", "0.05")
    with pytest.raises(ConfigError):
        load_config()
    load_dotenv(tmp_path / "missing.env")                                   # missing file is fine


# ------------------------------------------- backtest uses the same rules ----

def test_backtest_engine_follows_the_live_rule():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backtest"))
    from engine import shift_fill, simulate, stats
    d0 = date(2020, 1, 1)
    n = 700
    days = [d0 + timedelta(days=i) for i in range(n)]
    up = [100.0 * 1.002 ** i for i in range(400)]
    close = up + [up[-1] * 0.99 ** i for i in range(1, 301)]                # rise, then a long fall
    p = Params(breaker_drawdown=None)
    r = simulate(days, close, shift_fill(close, 0), days[0], days[-1], p, fee=0.0, slippage=0.0)
    assert r.weight[239] == 0.0                                             # no signal before 241 closes
    assert r.weight[260] == pytest.approx(1.0)                              # steady uptrend: fully invested
    assert r.weight[-1] == 0.0                                              # downtrend: out
    assert r.trades[0][1] == "buy" and r.trades[-1][1] == "sell"
    assert all(abs(t[3] - t[2]) >= 0.20 - 1e-9 or t[3] == 0.0 for t in r.trades)   # every trade crosses the band or exits
    s = stats(r)
    assert s["total"] > 1.0 and s["maxdd"] < 0.60                           # avoided most of the -95% fall
    # with a one-day delay nothing is filled at a price that was not yet known
    r1 = simulate(days, close, shift_fill(close, 1), days[0], days[-1], p, fee=0.001, slippage=0.0005)
    assert r1.trades[0][0] == r.trades[0][0] and stats(r1)["total"] < s["total"] * 1.01
