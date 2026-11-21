"""Signal maths, rebalance decision, order sizing and the circuit breaker."""
import math
import statistics
from datetime import date, timedelta

import pytest

from btc.strategy import (BreakerState, LOOKBACKS, Params, compute_signal, confirm_resume, decide, floor_to_step,
                          ceil_to_step, plan_order, realized_vol, update_breaker)


def series(n=300, start=100.0, step=1.0):
    return [start + i * step for i in range(n)]


# ---------------------------------------------------------------- signal ----

def test_defaults_are_the_agreed_rule():
    p = Params()
    assert p.lookbacks == (20, 40, 60, 90, 120, 180, 240)
    assert (p.vol_window, p.vol_target, p.band) == (30, 0.40, 0.20)
    assert p.down_penalty == 0.3 and p.breaker_half_max_days == 365
    assert (p.breaker_drawdown, p.breaker_pause_days, p.breaker_resume_scale) == (0.45, 30, 0.5)
    assert p.min_history == 241


def test_all_lookbacks_up_in_a_steady_uptrend():
    s = compute_signal(series())
    assert s.m == 7 and s.E == 1.0 and s.up == LOOKBACKS and s.down == ()
    assert s.V == 1.0          # tiny volatility -> capped at 1
    assert s.T == 1.0


def test_all_lookbacks_down_gives_zero_target():
    s = compute_signal(series(step=-0.2))
    assert s.m == 0 and s.T == 0.0 and s.down == LOOKBACKS


def test_partial_count_uses_each_lookback_separately():
    # flat at 100 for a long time, then the last 50 days at 110: 20 and 40 see no rise
    # (their reference is already 110); 60..240 look back to 100, so they are "up".
    c = [100.0] * 250 + [110.0] * 50
    s = compute_signal(c)
    assert s.up == (60, 90, 120, 180, 240) and s.down == (20, 40)
    assert s.E == pytest.approx((5 - 0.3 * 2) / 7)          # live rule: each "down" cancels 0.3 "up"
    assert s.E_plain == pytest.approx(5 / 7)                 # original rule, kept for the shadow ledger
    assert s.T_plain == pytest.approx(s.E_plain * s.V)
    assert compute_signal(c, Params(down_penalty=0.0)).E == pytest.approx(5 / 7)


def _closes_with_up_count(k):
    """241+ closes where exactly the k longest lookbacks are 'up'."""
    from btc.strategy import LOOKBACKS as LB
    c = [100.0] * 300
    c[-1] = 100.5
    for lb in LB[:7 - k]:                                    # make the shortest (7-k) lookbacks 'down'
        c[-1 - lb] = 101.0
    return c


@pytest.mark.parametrize("up,weight", [(0, 0.0), (1, 0.0), (2, 0.5 / 7), (3, 1.8 / 7), (4, 3.1 / 7), (5, 4.4 / 7),
                                        (6, 5.7 / 7), (7, 1.0)])
def test_weight_table_of_the_adopted_rule(up, weight):
    # owner's decision 2026-11-21: T = max(0, up - 0.3*down)/7 * V -> 0, 0, 0.07, 0.26, 0.44, 0.63, 0.81, 1 (x V)
    s = compute_signal(_closes_with_up_count(up))
    assert s.m == up
    assert s.E == pytest.approx(weight, abs=1e-12)
    assert round(s.E, 2) == [0, 0, 0.07, 0.26, 0.44, 0.63, 0.81, 1][up]
    assert s.T == pytest.approx(s.E * s.V) and s.E_plain == pytest.approx(up / 7)


def test_equal_price_is_not_up():
    c = [100.0] * 300
    assert compute_signal(c).m == 0          # strictly greater is required


def test_exact_reference_days():
    # close today is compared with the close exactly N days earlier (index -1-N)
    c = [100.0] * 300
    c[-1] = 100.5
    c[-1 - 20] = 101.0                       # only the 20-day reference is above today's close
    s = compute_signal(c)
    assert s.down == (20,) and s.m == 6


def test_vol_matches_textbook_formula():
    c = [100, 102, 101, 105, 103, 108, 107, 110, 109, 115, 112, 118, 117, 120, 119, 125, 123, 128, 126, 130,
         129, 133, 131, 136, 134, 139, 137, 142, 140, 145, 143]
    assert len(c) == 31
    rets = [math.log(b / a) for a, b in zip(c, c[1:])]
    expected = statistics.stdev(rets) * math.sqrt(365)
    assert realized_vol(c, 30) == pytest.approx(expected, rel=1e-12)


def test_vol_uses_only_the_last_30_returns():
    calm = [100.0 * (1.001 ** i) for i in range(31)]
    wild = [100.0, 150.0, 90.0, 160.0] + calm
    assert realized_vol(wild, 30) == pytest.approx(realized_vol(calm, 30), rel=1e-9)


def test_vol_target_scales_exposure():
    # alternate +4% / -3% days: strongly rising, high volatility
    c = [100.0]
    for i in range(299):
        c.append(c[-1] * (1.04 if i % 2 == 0 else 0.97))
    s = compute_signal(c)
    assert s.E == 1.0
    assert s.vol > 0.40
    assert s.V == pytest.approx(0.40 / s.vol)
    assert s.T == pytest.approx(s.V)
    assert compute_signal(c, Params(vol_target=None)).T == 1.0


def test_signal_ignores_older_history_and_needs_241_closes():
    c = series()
    assert compute_signal(c[-241:]) == compute_signal(c)
    with pytest.raises(ValueError):
        compute_signal(c[-240:])
    bad = list(c)
    bad[-5] = float("nan")
    with pytest.raises(ValueError):
        compute_signal(bad)
    bad[-5] = 0.0
    with pytest.raises(ValueError):
        compute_signal(bad)


# -------------------------------------------------------------- decision ----

@pytest.mark.parametrize("target,weight,action,reason", [
    (0.60, 0.40, "buy", "band"),            # exactly 0.20 apart -> trade
    (0.60, 0.41, "hold", "within_band"),
    (0.40, 0.60, "sell", "band"),
    (0.40, 0.59, "hold", "within_band"),
    (0.00, 0.05, "sell", "exit"),           # T = 0: sell everything, no band
    (0.00, 0.00, "hold", "already_flat"),
    (0.19, 0.00, "hold", "within_band"),    # small target from flat: wait
    (0.20, 0.00, "buy", "band"),
    (1.00, 0.85, "hold", "within_band"),
])
def test_decide(target, weight, action, reason):
    d = decide(target, weight, 0.20)
    assert (d.action, d.reason) == (action, reason)


def test_price_falling_never_triggers_a_buy():
    # the weight drops because price fell; with an unchanged target the gap must reach
    # the band before anything happens, and a lower target never causes a buy
    assert decide(0.50, 0.35, 0.20).action == "hold"
    assert decide(0.30, 0.35, 0.20).action == "hold"
    assert decide(0.00, 0.35, 0.20).action == "sell"


def test_dust_counts_as_flat():
    assert decide(0.0, 0.0004, 0.20, dust_weight=0.0005).action == "hold"
    assert decide(0.0, 0.0006, 0.20, dust_weight=0.0005).action == "sell"


# ---------------------------------------------------------- order sizing ----

KW = dict(bid=59999.9, ask=60000.0, lot=1e-8, min_size=1e-5, tick=0.1, slippage_cap=0.003, fee_rate=0.001)


def test_floor_and_ceil_to_step():
    assert floor_to_step(0.00123456789, 1e-8) == 0.00123456
    assert floor_to_step(0.3, 0.1) == 0.3                 # no float-noise rounding down to 0.2
    assert floor_to_step(56703.27, 0.1) == 56703.2
    assert ceil_to_step(56703.21, 0.1) == 56703.3
    assert ceil_to_step(56703.2, 0.1) == 56703.2
    assert floor_to_step(1e-5, 1e-8) == 1e-5


def test_buy_is_sized_to_target_and_rounded_to_lot_and_tick():
    p = plan_order(target=0.5, btc=0.0, usdt=120.0, **KW)
    assert p.side == "buy"
    assert p.size_btc == pytest.approx(60.0 / 60000.0, abs=2e-8)
    assert p.size_btc <= 60.0 / 60000.0
    assert round(p.size_btc / 1e-8) == pytest.approx(p.size_btc / 1e-8, abs=1e-6)      # on the lot grid
    assert p.limit_price == pytest.approx(60180.0)                                      # ask * 1.003 on the tick grid
    assert round(p.limit_price / 0.1) == pytest.approx(p.limit_price / 0.1, abs=1e-6)


def test_buy_can_never_cost_more_than_the_cash_even_at_the_worst_price():
    p = plan_order(target=1.0, btc=0.0, usdt=120.0, **KW)
    assert p.side == "buy"
    assert p.size_btc * p.limit_price * 1.001 <= 120.0
    assert p.size_btc * p.limit_price * 1.001 > 119.9      # ...but it does use the cash


def test_sell_is_capped_by_holdings_and_full_exit_sells_everything():
    p = plan_order(target=0.0, btc=0.00123456789, usdt=10.0, **KW)
    assert p.side == "sell" and p.size_btc == 0.00123456       # all of it, rounded down to the lot
    assert p.limit_price == pytest.approx(59819.9)             # bid * 0.997, rounded down to the tick
    q = plan_order(target=0.25, btc=0.001, usdt=60.0, **KW)    # 50% -> 25%
    assert q.side == "sell" and 0 < q.size_btc < 0.001


def test_orders_below_exchange_minimum_are_skipped():
    p = plan_order(target=1.0, btc=0.0, usdt=0.5, **KW)        # 0.5 USDT ~ 0.0000083 BTC < 0.00001
    assert p.side == "none" and p.skip_reason == "below_min_size"
    q = plan_order(target=1.0, btc=0.0, usdt=0.9, min_notional=1.0, **KW)
    assert q.side == "none" and q.skip_reason == "below_min_notional"


def test_respects_available_balance_and_order_cap():
    p = plan_order(target=1.0, btc=0.0, usdt=120.0, usdt_avail=30.0, **KW)
    assert p.size_btc * p.limit_price * 1.001 <= 30.0
    q = plan_order(target=1.0, btc=0.0, usdt=120.0, max_notional=50.0, **KW)
    assert q.notional <= 50.0
    s = plan_order(target=0.0, btc=0.002, usdt=0.0, btc_avail=0.001, **KW)
    assert s.size_btc == 0.001


def test_bad_quote_is_refused():
    kw = dict(KW, bid=60000.0, ask=59000.0)
    assert plan_order(target=1.0, btc=0.0, usdt=120.0, **kw).side == "none"


# ------------------------------------------------------- circuit breaker ----

D0 = date(2026, 1, 1)


def test_breaker_trips_at_45_percent_from_peak():
    b = BreakerState()
    b, scale, ev = update_breaker(b, 1.00, D0)
    b, scale, ev = update_breaker(b, 2.00, D0 + timedelta(days=1))      # new peak
    assert (scale, ev, b.peak_nav) == (1.0, "", 2.0)
    b, scale, ev = update_breaker(b, 1.11, D0 + timedelta(days=2))      # -44.5%
    assert (scale, ev, b.status) == (1.0, "", "normal")
    b, scale, ev = update_breaker(b, 1.10, D0 + timedelta(days=3))      # -45.0% exactly
    assert (scale, ev, b.status) == (0.0, "tripped", "paused")
    assert b.resume_on == (D0 + timedelta(days=33)).isoformat()


def test_breaker_pauses_30_days_then_half_until_confirmed():
    b = BreakerState(peak_nav=2.0)
    b, _, _ = update_breaker(b, 1.0, D0)
    assert b.status == "paused"
    for k in range(1, 30):
        b, scale, ev = update_breaker(b, 1.0, D0 + timedelta(days=k))
        assert scale == 0.0 and ev == "" and b.status == "paused"
    b, scale, ev = update_breaker(b, 1.0, D0 + timedelta(days=30))
    assert (scale, ev, b.status) == (0.5, "pause_ended", "half")
    b, scale, ev = update_breaker(b, 1.3, D0 + timedelta(days=60))
    assert scale == 0.5 and b.status == "half"                          # stays half without the owner
    b = confirm_resume(b)
    b, scale, ev = update_breaker(b, 1.3, D0 + timedelta(days=61))
    assert scale == 1.0 and b.status == "normal"


def test_breaker_measures_drawdown_afresh_after_the_pause():
    b = BreakerState(peak_nav=2.0)
    b, _, _ = update_breaker(b, 1.0, D0)
    b, scale, _ = update_breaker(b, 1.0, D0 + timedelta(days=30))
    assert b.peak_nav == 1.0 and scale == 0.5                           # not instantly re-tripped
    b, scale, ev = update_breaker(b, 0.55, D0 + timedelta(days=40))     # a further -45% trips again
    assert ev == "tripped" and scale == 0.0


def test_confirm_only_valid_in_half_state():
    with pytest.raises(ValueError):
        confirm_resume(BreakerState())
    with pytest.raises(ValueError):
        confirm_resume(BreakerState(status="paused"))


def test_breaker_can_be_disabled_for_backtests():
    b, scale, ev = update_breaker(BreakerState(peak_nav=2.0), 0.5, D0, Params(breaker_drawdown=None))
    assert scale == 1.0 and ev == ""
