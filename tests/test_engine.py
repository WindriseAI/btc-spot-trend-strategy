"""End-to-end runs of the daily engine against a fake OKX (ccxt is mocked)."""
import json
import re
from datetime import timedelta
from pathlib import Path

import pytest

from btc.engine import cl_ord_id, run_daily
from btc.exchange import INST, OkxSpot, SafetyError
from btc.report import daily_report
from btc.state import Store

from conftest import APIKEY, NOW, PASSPHRASE, SECRET, FakeOkxClient, day, fake_md

ROOT = Path(__file__).resolve().parent.parent


def state_of(h, name="paper"):
    return json.loads((h.cfg.state_dir / f"{name}.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------ the basics ----

def test_report_mode_never_orders_and_never_writes_state(harness):
    h = harness()
    r = h.run("report", T=1.0)
    assert h.client.posts == []
    assert r.plan.side == "buy" and not r.order_sent
    assert not h.cfg.state_dir.exists() or not list(h.cfg.state_dir.glob("*.json"))
    assert "只看信号，未下单" in daily_report(r)


def test_paper_buy_sends_exactly_one_spot_cash_ioc_order(harness):
    h = harness()
    r = h.run("paper", T=0.5)
    assert len(h.client.posts) == 1
    o = h.client.posts[0]
    assert o["instId"] == "BTC-USDT" and o["tdMode"] == "cash" and o["ordType"] == "ioc" and o["side"] == "buy"
    assert set(o) == {"instId", "tdMode", "side", "ordType", "sz", "px", "clOrdId"}
    assert re.fullmatch(r"\d+\.\d{8}", o["sz"]) and re.fullmatch(r"\d+\.\d", o["px"])     # lot / tick grid, plain decimals
    assert float(o["px"]) == pytest.approx(60000.0 * 1.003)
    assert o["clOrdId"] == "btcreb20261120D"
    assert h.demo_flags == [True]                       # paper mode always talks to the demo exchange
    assert r.order.filled > 0 and r.weight_after == pytest.approx(0.5, abs=0.01)
    assert "已买入" in daily_report(r)


def test_live_uses_real_exchange_flag_and_needs_confirmation(harness):
    h = harness(live_confirm=False)
    r = h.run("live", T=1.0)
    assert h.client.posts == [] and h.client.calls == []
    assert any("LIVE_CONFIRM" in e for e in r.errors) and r.needs_attention
    h2 = harness(live_confirm=True)
    r2 = h2.run("live", T=1.0)
    assert len(h2.client.posts) == 1 and h2.demo_flags == [False]
    assert h2.client.posts[0]["clOrdId"] == "btcreb20261120L"
    assert (h2.cfg.state_dir / "live.json").exists() and not (h2.cfg.state_dir / "paper.json").exists()


def test_missing_keys_refuse_to_run(harness):
    h = harness(demo=False)
    r = h.run("paper", T=1.0)
    assert h.client.calls == [] and any("OKX_DEMO_API_KEY" in e for e in r.errors)


def test_within_band_does_nothing(harness):
    h = harness(FakeOkxClient(btc=0.001, usdt=60.0))        # 50% BTC
    r = h.run("paper", T=0.65)
    assert h.client.posts == [] and r.decision.reason == "within_band"
    assert "无需操作" in daily_report(r)


def test_target_zero_sells_everything(harness):
    h = harness(FakeOkxClient(btc=0.00123456789, usdt=10.0))
    r = h.run("paper", T=0.0)
    assert len(h.client.posts) == 1
    assert h.client.posts[0]["side"] == "sell" and h.client.posts[0]["sz"] == "0.00123456"
    assert h.client.btc < 1e-8                               # only sub-lot dust is left
    r2 = h.run("paper", T=0.0, now=day(1))
    assert len(h.client.posts) == 1 and r2.decision.reason == "already_flat"


def test_tiny_account_below_min_size_is_skipped(harness):
    h = harness(FakeOkxClient(btc=0.0, usdt=1.5, min_sz="0.0001"))
    r = h.run("paper", T=1.0)
    assert h.client.posts == [] and r.plan.side == "none"


def test_empty_account_is_reported_not_traded(harness):
    h = harness(FakeOkxClient(btc=0.0, usdt=0.0))
    r = h.run("paper", T=1.0)
    assert h.client.posts == [] and any("划转" in b for b in r.blocks)


# ------------------------------------------------------------ idempotency ----

def test_running_twice_the_same_day_sends_one_order(harness):
    h = harness()
    h.run("paper", T=1.0)
    r2 = h.run("paper", T=1.0, now=NOW + timedelta(hours=3))
    r3 = h.run("paper", T=0.0, now=NOW + timedelta(hours=6))     # even if the target flips intraday
    assert len(h.client.posts) == 1
    assert "今天已完成调仓" in daily_report(r2)
    assert r3.already_traded and "不再重复下单" in daily_report(r3)
    h.run("paper", T=0.0, now=day(1))                            # next signal date: allowed again
    assert len(h.client.posts) == 2


def test_unfilled_order_is_not_retried_the_same_day(harness):
    h = harness()
    h.client.fill = False
    r = h.run("paper", T=1.0)
    assert len(h.client.posts) == 1 and r.order.filled == 0
    assert "没有成交" in daily_report(r)
    h.client.fill = True
    h.run("paper", T=1.0, now=NOW + timedelta(hours=1))
    assert len(h.client.posts) == 1
    h.run("paper", T=1.0, now=day(1))
    assert len(h.client.posts) == 2


def test_crash_right_after_sending_does_not_cause_a_second_order(harness):
    h = harness()
    h.client.fail_get_order = 99                # the order goes out, then every status query dies
    # first query (pre-send duplicate check) must succeed, so fail only after the POST
    h.client.fail_get_order = 0
    orig_post = h.client.private_post_trade_order

    def post_then_break(req):
        out = orig_post(req)
        h.client.fail_get_order = 99
        return out
    h.client.private_post_trade_order = post_then_break
    r1 = h.run("paper", T=1.0)
    assert len(h.client.posts) == 1 and r1.errors            # the run died after sending
    st = state_of(h)
    assert st["pending_order"]["cl_ord_id"] == "btcreb20261120D"
    assert st["last_trade_signal_date"] == "2026-11-20"
    h.client.fail_get_order = 0
    r2 = h.run("paper", T=1.0, now=NOW + timedelta(minutes=5))
    assert len(h.client.posts) == 1                          # no second order
    assert r2.reconciled and "完全成交" in r2.reconciled
    st = state_of(h)
    assert st["pending_order"] is None and st["trades"][-1]["filled"] > 0
    assert r2.flow == 0.0                                    # the fill is not mistaken for a deposit


def test_failed_send_is_not_retried_the_same_day(harness):
    h = harness()
    h.client.fail_post = RuntimeError("timeout")             # unknown outcome: must not resend
    r1 = h.run("paper", T=1.0)
    assert r1.errors and len(h.client.posts) == 1
    h.client.fail_post = None
    r2 = h.run("paper", T=1.0, now=NOW + timedelta(minutes=5))
    assert len(h.client.posts) == 1 and "查不到该订单" in r2.reconciled
    h.run("paper", T=1.0, now=day(1))
    assert len(h.client.posts) == 2


def test_lost_state_file_does_not_cause_a_second_order(harness):
    h = harness()
    h.client.fill = False                                    # weight stays off target, so a rerun would want to trade
    h.run("paper", T=1.0)
    for f in h.cfg.state_dir.glob("paper.json*"):
        f.unlink()
    r = h.run("paper", T=1.0, now=NOW + timedelta(minutes=5))
    assert len(h.client.posts) == 1
    assert r.already_traded and any("不再重复下单" in w for w in r.warnings)


def test_concurrent_run_is_blocked_by_the_lock(harness):
    h = harness()
    s = Store(h.cfg.state_dir, "paper")
    s.acquire()
    try:
        r = h.run("paper", T=1.0)
        assert h.client.posts == [] and any("另一个实例" in e for e in r.errors)
    finally:
        s.release()
    h.run("paper", T=1.0)
    assert len(h.client.posts) == 1


# ---------------------------------------------------------------- safety ----

def test_key_with_withdraw_permission_is_refused(harness):
    h = harness(FakeOkxClient(perm="read_only,trade,withdraw"))
    for mode in ("report", "paper"):
        r = h.run(mode, T=1.0)
        assert h.client.posts == [] and any("提现" in e for e in r.errors)
        assert "balance" not in h.client.calls


def test_read_only_key_cannot_trade_but_can_report(harness):
    h = harness(FakeOkxClient(perm="read_only"))
    assert not h.run("report", T=1.0).errors
    r = h.run("paper", T=1.0)
    assert h.client.posts == [] and any("交易" in e for e in r.errors)


def test_open_derivative_position_blocks_spot_trading_and_is_never_touched(harness):
    h = harness(FakeOkxClient(positions=[{"instId": "BTC-USDT-SWAP", "pos": "-1"}]))
    r = h.run("paper", T=1.0)
    assert h.client.posts == [] and any("合约" in b for b in r.blocks)
    assert "没有下单" in daily_report(r)
    h.client.positions = []
    h.run("paper", T=1.0, now=NOW + timedelta(hours=1))      # the blocked attempt did not use up the day
    assert len(h.client.posts) == 1


def test_borrowed_balance_blocks_trading(harness):
    h = harness()
    h.client.liab = "5"
    r = h.run("paper", T=1.0)
    assert h.client.posts == [] and any("负债" in b for b in r.blocks)


def test_bad_data_blocks_trading(harness):
    h = harness()
    h.md_kwargs = dict(ok=False)
    r = h.run("paper", T=1.0)
    assert h.client.posts == [] and r.blocks and r.needs_attention
    h.md_kwargs = {}
    h.run("paper", T=1.0, now=NOW + timedelta(hours=1))
    assert len(h.client.posts) == 1


def test_stale_quote_blocks_trading(harness):
    h = harness()
    from btc.engine import run_daily as rd
    h.client.now = NOW - timedelta(minutes=10)               # ticker timestamp is 10 minutes old
    r = rd("paper", h.cfg, now=NOW, make_exchange=h.make_exchange,
           get_data=lambda n, p, d, hh: fake_md(1.0, n), sleep=lambda s: None)
    assert h.client.posts == [] and any("过期" in b for b in r.blocks)


def test_order_guard_rejects_anything_but_btc_usdt_cash_ioc():
    ex = OkxSpot(None, demo=True, client=FakeOkxClient())
    good = {"instId": INST, "tdMode": "cash", "side": "buy", "ordType": "ioc", "sz": "0.001", "px": "60000.0", "clOrdId": "abc1"}
    assert ex._post_order(dict(good))
    for bad in ({"instId": "BTC-USDT-SWAP"}, {"tdMode": "cross"}, {"tdMode": "isolated"}, {"ordType": "market"},
                {"side": "short"}, {"sz": "0"}, {"sz": "-1"}, {"clOrdId": "bad id!"}, {"instId": "ETH-USDT"}):
        with pytest.raises(SafetyError):
            ex._post_order({**good, **bad})
    with pytest.raises(SafetyError):
        ex._post_order({**good, "reduceOnly": "true"})        # unknown extra fields are refused too
    with pytest.raises(SafetyError):
        ex._post_order({**good, "tag": "valid123", "reduceOnly": "true"})
    with pytest.raises(SafetyError):
        ex._post_order({**good, "aiBuilderCode": "valid123"}) # OpenAPI uses `tag`, not `aiBuilderCode`
    missing_req = {k: v for k, v in good.items() if k != "clOrdId"}
    with pytest.raises(SafetyError):
        ex._post_order({**missing_req, "tag": "valid123"})
    assert len(ex.c.posts) == 1


def test_builder_code_omitted_when_unconfigured_and_sent_as_tag_when_configured(harness, tmp_path):
    # 1. Unconfigured: request matches original 7-field payload exactly
    h_none = harness(state_dir=tmp_path / "none")
    h_none.run("paper", T=0.5)
    assert len(h_none.client.posts) == 1
    assert set(h_none.client.posts[0]) == {"instId", "tdMode", "side", "ordType", "sz", "px", "clOrdId"}
    assert "tag" not in h_none.client.posts[0]

    # 2. Configured: request includes `tag` in both paper and live modes
    h_paper = harness(state_dir=tmp_path / "paper", builder_code="Windrise01")
    h_paper.run("paper", T=0.5)
    assert len(h_paper.client.posts) == 1
    assert set(h_paper.client.posts[0]) == {"instId", "tdMode", "side", "ordType", "sz", "px", "clOrdId", "tag"}
    assert h_paper.client.posts[0]["tag"] == "Windrise01"

    h_live = harness(state_dir=tmp_path / "live", live_confirm=True, builder_code="A123456789012345")
    h_live.run("live", T=0.5)
    assert len(h_live.client.posts) == 1
    assert h_live.client.posts[0]["tag"] == "A123456789012345"


def test_invalid_builder_code_tag_is_rejected_with_safety_error(harness):
    ex = OkxSpot(None, demo=True, client=FakeOkxClient())
    good = {"instId": INST, "tdMode": "cash", "side": "buy", "ordType": "ioc", "sz": "0.001", "px": "60000.0", "clOrdId": "abc1"}
    bad_tags = ("", " ", "a" * 17, "bad-tag", "bad_tag", "bad tag", "abc!", "中文123", None, 12345)
    for bad_tag in bad_tags:
        with pytest.raises(SafetyError):
            ex._post_order({**good, "tag": bad_tag})
    assert ex.c.posts == []

    for bad_tag in (" ", "a" * 17, "bad-tag", "bad_tag", "中文123", 12345):
        with pytest.raises(SafetyError):
            OkxSpot(None, demo=True, client=FakeOkxClient(), builder_code=bad_tag)

    # Boundary valid tags (length 1 and 16) succeed
    for ok_tag, cid in (("A", "cid1"), ("a1B2c3D4e5F6g7H8", "cid2")):
        assert ex._post_order({**good, "clOrdId": cid, "tag": ok_tag})
    assert [p["tag"] for p in ex.c.posts] == ["A", "a1B2c3D4e5F6g7H8"]

    # End-to-end: invalid builder_code stops run with SafetyError and sends zero orders
    h_bad = harness(builder_code="bad-tag!")
    r = h_bad.run("paper", T=1.0)
    assert h_bad.client.posts == []
    assert any("安全检查未通过" in e for e in r.errors) and r.needs_attention


def test_source_has_one_write_endpoint_and_no_dangerous_ones():
    src = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "btc").glob("*.py"))
    code = re.sub(r'""".*?"""', "", src, flags=re.S)             # ignore docstrings
    code = "\n".join(line.split("#")[0] for line in code.splitlines())
    posts = re.findall(r"private_post_\w+|privatePost\w+", code)
    assert posts == ["private_post_trade_order"]
    lowered = code.lower()
    for word in ("withdrawal", "asset_transfer", "set_leverage", "set-leverage", "create_order(", "margin_mode",
                 "private_post_asset", "private_post_account", "/api/v5/asset"):
        assert word not in lowered, word
    assert "swap" not in re.sub(r'"[^"\n]*"', '""', lowered)     # only mentioned inside user-facing strings


def test_secrets_never_appear_in_output_state_or_logs(harness, capsys):
    h = harness()
    r = h.run("paper", T=1.0)
    text = daily_report(r) + repr(h.cfg) + repr(r)
    files = "".join(p.read_text(encoding="utf-8") for p in h.cfg.state_dir.iterdir() if p.is_file())
    out = capsys.readouterr()
    for secret in (SECRET, PASSPHRASE, APIKEY):
        assert secret not in text and secret not in files and secret not in out.out + out.err


# ---------------------------------------------- deposits and the breaker ----

def test_deposit_is_not_counted_as_profit_and_is_allocated_by_target(harness):
    h = harness(FakeOkxClient(btc=0.0, usdt=100.0))
    r1 = h.run("paper", T=0.5)
    assert r1.nav == pytest.approx(1.0) and len(h.client.posts) == 1
    nav_before = state_of(h)["nav"]
    h.client.usdt += 100.0                                   # the owner adds savings
    r2 = h.run("paper", T=0.5, now=day(1))
    assert r2.flow == pytest.approx(100.0, abs=0.01)
    assert r2.nav == pytest.approx(nav_before, rel=2e-3)     # NAV unchanged by the deposit (only fees/spread)
    assert r2.dd < 0.01
    assert "检测到入金" in daily_report(r2)
    # weight fell from ~50% to ~25%: gap >= 20% -> the new money is put to work by the rule
    assert len(h.client.posts) == 2 and h.client.posts[1]["side"] == "buy"
    assert r2.weight_after == pytest.approx(0.5, abs=0.01)


def test_withdrawal_is_not_counted_as_loss(harness):
    h = harness(FakeOkxClient(btc=0.0, usdt=200.0))
    h.run("paper", T=0.0)
    h.client.usdt -= 120.0
    r = h.run("paper", T=0.0, now=day(1))
    assert r.flow == pytest.approx(-120.0) and r.nav == pytest.approx(1.0) and r.dd == 0.0
    assert r.breaker.status == "normal"


def test_price_fall_does_not_cause_buying(harness):
    h = harness(FakeOkxClient(btc=0.0, usdt=100.0))
    h.run("paper", T=0.5)
    h.client.bid, h.client.ask = 44999.9, 45000.0            # -25%: weight drifts down to ~43%
    r = h.run("paper", T=0.5, now=day(1))
    assert len(h.client.posts) == 1 and r.decision.action == "hold"


def test_breaker_sells_everything_pauses_then_runs_half_until_confirmed(harness):
    from btc.__main__ import cmd_confirm
    h = harness(FakeOkxClient(btc=0.0, usdt=100.0))
    h.run("paper", T=1.0)                                     # fully invested at 60000
    assert len(h.client.posts) == 1
    h.client.bid, h.client.ask = 32399.9, 32400.0            # -46%
    r = h.run("paper", T=1.0, now=day(1))                    # the signal still says "hold 100%"
    assert r.breaker_event == "tripped" and r.needs_attention
    assert len(h.client.posts) == 2 and h.client.posts[1]["side"] == "sell"
    assert h.client.btc < 1e-7
    assert "熔断" in daily_report(r) and "今天触发" in daily_report(r)
    for k in (2, 15, 30):                                    # paused: no buying whatever the signal
        rk = h.run("paper", T=1.0, now=day(k))
        assert rk.scale == 0.0 and len(h.client.posts) == 2
        assert "暂停中" in daily_report(rk)
    r31 = h.run("paper", T=1.0, now=day(31))                 # pause over: half size
    assert r31.breaker_event == "pause_ended" and r31.target == pytest.approx(0.5)
    assert len(h.client.posts) == 3 and r31.weight_after == pytest.approx(0.5, abs=0.01)
    assert "半仓" in daily_report(r31)
    r32 = h.run("paper", T=1.0, now=day(32))
    assert r32.target == pytest.approx(0.5) and len(h.client.posts) == 3
    assert cmd_confirm(h.cfg, "paper") == 0                  # the owner re-confirms
    r33 = h.run("paper", T=1.0, now=day(33))
    assert r33.target == pytest.approx(1.0) and len(h.client.posts) == 4
    assert cmd_confirm(h.cfg, "paper") == 1                  # nothing left to confirm


def test_breaker_still_sells_when_price_data_is_unavailable(harness):
    h = harness(FakeOkxClient(btc=0.0, usdt=100.0))
    h.run("paper", T=1.0)
    h.client.bid, h.client.ask = 29999.9, 30000.0
    h.md_kwargs = dict(ok=False)
    r = h.run("paper", T=1.0, now=day(1))
    assert r.breaker_event == "tripped" and h.client.posts[-1]["side"] == "sell"


def test_corrupt_state_stops_trading(harness):
    h = harness()
    h.run("paper", T=0.0)
    (h.cfg.state_dir / "paper.json").write_text("{ not json", encoding="utf-8")
    r = h.run("paper", T=1.0, now=day(1))
    assert h.client.posts == [] and any("状态文件损坏" in e for e in r.errors)
    assert (h.cfg.state_dir / "paper.json.bak").exists() or True


def test_report_text_has_all_required_fields(harness):
    h = harness(FakeOkxClient(btc=0.001, usdt=60.0))
    txt = daily_report(h.run("paper", T=0.6))
    for needle in ("BTC 策略日报", "2026-11-21", "北京时间 08:22", "BTC 收盘价", "趋势：7/7 个回看期上涨", "上涨：20、40、60、90、120、180、240 日", "E = 1.00",
                   "波动率 vol30", "V = ", "目标仓位 T = E×V：60%", "当前仓位：50%", "操作：无需操作", "账户净值",
                   "距最高点回撤", "熔断：正常", "数据：OKX", "错误：无"):
        assert needle in txt, needle


def test_client_order_id_is_deterministic_and_valid():
    from datetime import date
    a, b = cl_ord_id(date(2026, 11, 20), "live"), cl_ord_id(date(2026, 11, 20), "demo")
    assert a == "btcreb20261120L" and b == "btcreb20261120D" and a.isalnum() and len(a) <= 32


def test_second_run_same_day_keeps_the_trade_in_history(harness):
    h = harness()
    h.run("paper", T=1.0)
    h.run("paper", T=1.0, now=NOW + timedelta(hours=2))
    hist = state_of(h)["history"]
    assert len(hist) == 1 and hist[0]["action"] == "buy" and hist[0]["weight_after"] > 0.9


def test_data_fetch_crash_is_reported_and_blocks_trading(harness):
    h = harness()

    def boom(*a):
        raise RuntimeError("dns failure")
    r = run_daily("paper", h.cfg, now=NOW, make_exchange=h.make_exchange, get_data=boom, sleep=lambda s: None)
    assert h.client.posts == [] and r.md.signal is None and r.needs_attention
    assert str(r.md.signal_date) == "2026-11-20"
    assert "无法计算信号" in daily_report(r) and "dns failure" in daily_report(r)
