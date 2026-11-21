"""Shadow ledgers (three books), the rule replay and the 12-month breaker rule."""
from datetime import date, timedelta

import pytest

from btc import ledger
from btc.report import daily_report, weekly_report
from btc.strategy import BreakerState, Params, update_breaker

from conftest import NOW, FakeOkxClient, day

D0 = date(2026, 11, 21)
FEE = 0.001


def test_init_gives_all_ledgers_the_same_starting_money():
    sh = ledger.init(0.0, 100.0, 60000.0, FEE, D0)
    assert sh["paid_in"] == 100.0
    assert ledger.value(sh["dca"], 60000.0) == pytest.approx(99.9)          # all in BTC, one fee
    assert sh["dca"]["usdt"] == pytest.approx(0.0)
    assert sh["half"]["btc"] * 60000.0 == pytest.approx(50.0 * (1 - FEE))
    assert sh["half"]["usdt"] == pytest.approx(50.0)
    assert sh["replay"] == {"btc": 0.0, "usdt": 100.0, "last_date": None}   # starts exactly as the account is
    assert sh["old"] == sh["replay"] and sh["old"] is not sh["replay"]


def test_deposits_and_withdrawals_hit_every_ledger_equally():
    sh = ledger.init(0.0, 100.0, 60000.0, FEE, D0)
    before = {k: ledger.value(sh[k], 60000.0) for k in ledger.NAMES}
    ledger.apply_flow(sh, 50.0, 60000.0, FEE)
    assert sh["paid_in"] == 150.0
    for k in ledger.NAMES:
        assert ledger.value(sh[k], 60000.0) == pytest.approx(before[k] + 50.0, abs=0.06)   # minus at most one fee
    assert sh["dca"]["usdt"] == pytest.approx(0.0)                           # deposit went straight into BTC
    assert sh["replay"]["usdt"] == pytest.approx(150.0)                      # the rule decides later
    ledger.apply_flow(sh, -120.0, 60000.0, FEE)                              # withdrawal bigger than any cash pile
    assert sh["paid_in"] == pytest.approx(30.0)
    for k in ledger.NAMES:
        assert ledger.value(sh[k], 60000.0) == pytest.approx(before[k] + 50.0 - 120.0, abs=0.25)
        assert sh[k]["usdt"] >= 0 and sh[k]["btc"] >= 0


def test_half_ledger_rebalances_only_when_the_month_changes():
    sh = ledger.init(0.0, 100.0, 60000.0, FEE, date(2026, 11, 21))
    ledger.step(sh, date(2026, 11, 30), 90000.0, FEE)
    w = sh["half"]["btc"] * 90000.0 / ledger.value(sh["half"], 90000.0)
    assert w == pytest.approx(0.6, abs=0.01)                                 # drifted, untouched within the month
    ledger.step(sh, date(2026, 12, 1), 90000.0, FEE)
    w = sh["half"]["btc"] * 90000.0 / ledger.value(sh["half"], 90000.0)
    assert w == pytest.approx(0.5, abs=0.001) and sh["half"]["month"] == "2026-12"


def test_replay_follows_the_rule_and_catches_up_missed_days():
    sh = ledger.init(0.0, 100.0, 60000.0, FEE, D0)
    d1, d2, d3 = D0, D0 + timedelta(days=1), D0 + timedelta(days=2)
    ledger.step_replay(sh, [(d1, 60000.0, 0.10)], 60000.0, 1.0, 0.20, FEE)   # below the band: nothing
    assert sh["replay"]["btc"] == 0.0 and sh["replay"]["last_date"] == d1.isoformat()
    ledger.step_replay(sh, [(d1, 60000.0, 0.10)], 60000.0, 1.0, 0.20, FEE)   # same date again: no double step
    # the script missed d2 (target 0.6 at close 50000); today is d3 (target 0.6, price now 55000)
    ledger.step_replay(sh, [(d1, 60000.0, 0.10), (d2, 50000.0, 0.60), (d3, 54000.0, 0.60)], 55000.0, 1.0, 0.20, FEE)
    r = sh["replay"]
    assert r["btc"] == pytest.approx(60.0 * (1 - FEE) / 50000.0)             # bought on the missed day, at its close
    assert r["last_date"] == d3.isoformat()
    ledger.step_replay(sh, [(d3 + timedelta(days=1), 50000.0, 0.0)], 50000.0, 1.0, 0.20, FEE)
    assert sh["replay"]["btc"] == 0.0                                        # target 0: out
    ledger.step_replay(sh, [(d3 + timedelta(days=2), 50000.0, 1.0)], 50000.0, 0.0, 0.20, FEE)
    assert sh["replay"]["btc"] == 0.0                                        # breaker scale 0 applies to the replay too


def test_old_rule_ledger_uses_its_own_target():
    sh = ledger.init(0.0, 100.0, 60000.0, FEE, D0)
    # new rule says 26% (3 of 7 up), the old rule says 43%: both cross the band from flat
    ledger.step_replay(sh, [(D0, 60000.0, 0.26, 0.43)], 60000.0, 1.0, 0.20, FEE)
    w = lambda l: l["btc"] * 60000.0 / ledger.value(l, 60000.0)
    assert w(sh["replay"]) == pytest.approx(0.26, abs=0.001) and w(sh["old"]) == pytest.approx(0.43, abs=0.001)
    # new rule 7% (2 of 7 up) vs old 29%: the new rule stays out, the old one buys
    sh2 = ledger.init(0.0, 100.0, 60000.0, FEE, D0)
    ledger.step_replay(sh2, [(D0, 60000.0, 0.07, 0.29)], 60000.0, 1.0, 0.20, FEE)
    assert sh2["replay"]["btc"] == 0.0 and w(sh2["old"]) == pytest.approx(0.29, abs=0.001)
    s = ledger.summary(sh2, 60000.0, 100.0)
    assert set(s) == {"paid_in", "live", "dca", "half", "old", "replay", "replay_dev"}
    legacy = {k: v for k, v in ledger.init(0.0, 100.0, 60000.0, FEE, D0).items() if k != "old"}
    ledger.apply_flow(legacy, 10.0, 60000.0, FEE)                            # older state files gain the ledger
    assert legacy["old"]["usdt"] == pytest.approx(110.0)


def test_engine_keeps_three_ledgers_and_flags_execution_gaps(harness):
    h = harness(FakeOkxClient(btc=0.0, usdt=100.0))
    r1 = h.run("paper", T=1.0)
    led = r1.ledgers
    assert led["paid_in"] == pytest.approx(100.0)
    assert led["dca"] == pytest.approx(99.9, abs=0.05) and led["half"] == pytest.approx(99.95, abs=0.05)
    assert abs(led["replay_dev"]) < 0.005
    # day 2: the price rises 10% but our order does not fill for 1 day -> no gap yet (already invested)
    h.client.bid, h.client.ask = 65999.9, 66000.0
    h.client.usdt += 100.0                                                   # deposit: all ledgers get it
    r2 = h.run("paper", T=1.0, now=day(1))
    assert r2.ledgers["paid_in"] == pytest.approx(200.0, abs=0.01)
    assert abs(r2.ledgers["replay_dev"]) < 0.01
    assert r2.ledgers["dca"] == pytest.approx(r2.ledgers["live"], rel=0.01)  # both fully invested throughout
    assert r2.ledgers["half"] < r2.ledgers["live"]
    # days 3-4: orders fail (exchange cancels them) while the rule says "sell everything" and price drops 10%
    h.client.fill = False
    h.run("paper", T=0.0, now=day(2))
    h.client.bid, h.client.ask = 58999.9, 59000.0
    r4 = h.run("paper", T=0.0, now=day(3))
    assert r4.ledgers["replay_dev"] < -0.02                                  # the replay got out, the account did not
    assert any("规则重放" in w for w in r4.warnings)
    assert "规则重放" in daily_report(r4)


def test_weekly_report_shows_three_ledgers_and_expectations(harness):
    import json
    from btc import data as D
    from btc.strategy import compute_signal
    h = harness(FakeOkxClient(btc=0.0, usdt=100.0))
    h.run("paper", T=0.5)
    h.run("paper", T=0.5, now=day(1))
    state = json.loads((h.cfg.state_dir / "paper.json").read_text(encoding="utf-8"))
    last = (day(1)).date() - timedelta(days=1)
    closes = [60000.0 + i for i in range(249)]
    rows = D.recent_signals(closes, last, 8)
    txt = weekly_report(rows, state, "demo", day(1), Params(), "OKX")
    for needle in ("四本账对比", "① 实盘（新权重）", "② 影子：旧权重", "③ 影子：存入就买、一直持有", "④ 影子：一半 BTC 一半 USDT，每月调平",
                   "规则重放", "正常（2% 以内）", "累计投入 100.00 USDT", "少亏", "10–20%", "-50%", "两三年不创新高"):
        assert needle in txt, needle
    assert rows and compute_signal


def test_half_size_ends_after_12_months_without_owner():
    b = BreakerState(peak_nav=2.0)
    b, _, _ = update_breaker(b, 1.0, D0)                                     # tripped
    b, scale, ev = update_breaker(b, 1.0, D0 + timedelta(days=30))
    assert (scale, ev, b.half_since) == (0.5, "pause_ended", (D0 + timedelta(days=30)).isoformat())
    b, scale, ev = update_breaker(b, 1.2, D0 + timedelta(days=30 + 364))
    assert (scale, ev, b.status) == (0.5, "", "half")
    b, scale, ev = update_breaker(b, 1.2, D0 + timedelta(days=30 + 365))
    assert (scale, ev, b.status) == (1.0, "auto_resumed", "normal") and b.half_since is None
    b, scale, ev = update_breaker(b, 1.2, D0 + timedelta(days=30 + 366))
    assert (scale, ev) == (1.0, "")


def test_second_trip_during_half_period_restarts_the_clock():
    b = BreakerState(peak_nav=2.0)
    b, _, _ = update_breaker(b, 1.0, D0)
    b, _, _ = update_breaker(b, 1.0, D0 + timedelta(days=30))                # half, peak reset to 1.0
    b, scale, ev = update_breaker(b, 0.5, D0 + timedelta(days=100))          # -50% again
    assert ev == "tripped" and b.status == "paused" and b.half_since is None
    b, scale, ev = update_breaker(b, 0.5, D0 + timedelta(days=130))
    assert ev == "pause_ended" and b.half_since == (D0 + timedelta(days=130)).isoformat()


def test_breaker_state_roundtrips_through_json():
    b = BreakerState("half", 1.5, "2026-01-01", None, 0.8, "2026-02-01")
    assert BreakerState.from_dict(b.to_dict()) == b
    assert BreakerState.from_dict({}).status == "normal"
    assert NOW


def test_replay_rows_cover_missed_days_from_the_long_close_window():
    from btc.data import MarketData
    from btc.engine import replay_rows
    from btc.strategy import compute_signal
    last = date(2026, 11, 20)
    closes = [100.0 + 0.5 * i for i in range(250)]                           # 9 spare days beyond the 241 needed
    md = MarketData(signal_date=last)
    md.closes, md.closes_long, md.signal = closes[-241:], closes, compute_signal(closes[-241:])
    assert [r[0] for r in replay_rows(md, None, Params())] == [last]         # first run: today only
    rows = replay_rows(md, (last - timedelta(days=3)).isoformat(), Params())
    assert [r[0] for r in rows] == [last - timedelta(days=2), last - timedelta(days=1), last]
    assert rows[0][1] == closes[-3] and rows[-1][1] == closes[-1]
    assert rows[0][2] == compute_signal(closes[-243:-2]).T and rows[0][3] == compute_signal(closes[-243:-2]).T_plain
    assert replay_rows(md, last.isoformat(), Params()) == []                 # already done today
    far = replay_rows(md, (last - timedelta(days=400)).isoformat(), Params())
    assert len(far) == 10                                                    # only what the data can reproduce
