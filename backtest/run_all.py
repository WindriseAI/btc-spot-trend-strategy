"""Run the whole verification and write docs/backtest-report.md (+ charts).

Usage:
    python backtest/fetch_data.py      # once, downloads public price data into data/
    python backtest/run_all.py         # ~6 minutes, pure Python

"new" = the rule the script trades (E = max(0, up - 0.3*down)/7, owner's decision
2026-11-21); "old" = the review's original proposal (E = up/7). Both are run
everywhere so they can be compared. Deterministic for a given data download.
"""
from __future__ import annotations

import json
import math
import random
import sys
import time
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from engine import (DATA, ROOT, align, gaps, hourly_fill_series, load_daily, load_hourly, patch_missing,  # noqa: E402
                    plan_stats, shift_fill, simulate, simulate_static, splice, stats, targets2, yearly)
from btc.strategy import LOOKBACKS, Params  # noqa: E402

END = "2026-11-20"
FEE, SLIP = 0.001, 0.0005          # OKX regular spot taker 0.10% + 0.05% slippage per trade
NB = dict(breaker_drawdown=None)   # most tables run without the breaker; section 9 tests it
P = Params(**NB)                   # new (live) rule
OLD = Params(down_penalty=0.0, **NB)
assert P.down_penalty == 0.3
DOCS = ROOT / "docs"
IMG = DOCS / "img"
R: dict = {}                       # everything computed, also dumped to data/bt_results.json
PERIODS = ("2015-10-01", "2018-01-01", "2020-01-01", "2022-01-01")


def pc(x, d=1):
    return "—" if x is None or x != x else f"{x * 100:.{d}f}%"


def pp(x, d=1):
    """signed percentage points"""
    return f"{x * 100:+.{d}f}"


def q(a, p_):
    a = sorted(a)
    return a[min(len(a) - 1, max(0, int(p_ * len(a))))]


def run(ds, s0, lag=1, p=P, fee=FEE, slip=SLIP, end=END, **kw):
    return simulate(ds.days, ds.close, shift_fill(ds.close, lag), s0, end, p, fee, slip, low=ds.low, **kw)


def cell(s):
    return f"{pc(s['cagr'])} / {pc(s['maxdd'], 0)}"


# ------------------------------------------------------------------ data ----
def load_all():
    y = load_daily("yahoo_btc.json")
    b = load_daily("bitstamp_btc.json")
    o = load_daily("okx_btc.json")
    y, patched = patch_missing(y, b)
    ya, ba = align(y, b)
    oa, bb = align(o, b)
    ya2, oa2 = align(y, o)

    def diffs(a, c, since=None):
        d = [abs(x / z - 1) for dd, x, z in zip(a.days, a.close, c.close) if since is None or dd >= since]
        return dict(n=len(d), median=q(d, .5), p99=q(d, .99), max=max(d))
    R["data"] = dict(
        yahoo=(str(y.days[0]), str(y.days[-1]), len(y), len(gaps(y))), bitstamp=(str(b.days[0]), str(b.days[-1]), len(b), len(gaps(b))),
        okx=(str(o.days[0]), str(o.days[-1]), len(o), len(gaps(o))), patched=[str(d) for d in patched],
        yahoo_vs_bitstamp=diffs(ya, ba, date(2018, 1, 11)), yahoo_vs_okx=diffs(ya2, oa2),
        okx_vs_bitstamp=diffs(oa, bb), okx_vs_bitstamp_2020=diffs(oa, bb, date(2020, 1, 1)))
    return y, b, splice(b, o)


# ------------------------------------------------------------- sections ----
def sec_review():
    try:
        from reproduce_review import reproduce
        R["review"] = {f"{k[0]}|{k[1]}|{k[2]}": v for k, v in reproduce().items()}
    except Exception as e:  # noqa: BLE001
        R["review"] = {"error": str(e)}


def sec_baseline(y, b, o, hourly):
    base = {}
    for tag, p in (("new", P), ("old", OLD)):
        for nm, ds in (("Yahoo", y), ("Bitstamp", b), ("OKX", o)):
            for s0 in PERIODS:
                if nm == "OKX" and s0 < "2018":
                    continue
                for lag in (0, 1):
                    base[f"{tag}|{nm}|{s0}|{lag}"] = stats(run(ds, s0, lag, p))
        for dh in (0, 1, 24):
            cl, fl = hourly_fill_series(y, hourly, dh)
            for s0 in ("2018-01-01", "2022-01-01"):
                base[f"{tag}|hourly{dh}|{s0}"] = stats(simulate(y.days, cl, fl, s0, END, p, FEE, SLIP))
    R["baseline"] = base
    bench = {}
    for s0 in PERIODS:
        bench[f"bh|{s0}"] = stats(simulate_static(y.days, y.close, s0, END))
        bench[f"half|{s0}"] = stats(simulate_static(y.days, y.close, s0, END, 0.5, rebalance_every=30))
        expo = base[f"new|Yahoo|{s0}|1"]["exposure"]
        bench[f"same_exposure|{s0}"] = stats(simulate_static(y.days, y.close, s0, END, expo, rebalance_every=30))
        bench[f"E_only|{s0}"] = stats(run(y, s0, 1, Params(vol_target=None, **NB)))
        bench[f"V_only|{s0}"] = stats(run(y, s0, 1, Params(lookbacks=(1,), down_penalty=0.0, **NB)))   # trend filter (almost) off
    R["bench"] = bench
    r = run(y, "2018-01-01", 1)
    r_old = run(y, "2018-01-01", 1, OLD)
    bh = simulate_static(y.days, y.close, "2018-01-01", END)
    yb, yo = yearly(bh), yearly(r_old)
    R["yearly"] = {str(k): (v, yo[k], yb[k]) for k, v in yearly(r).items()}
    eq, pk, start, longest = r.equity, r.equity[0], 0, 0
    for i, e in enumerate(eq):
        if e >= pk:
            pk, start = e, i
        longest = max(longest, i - start)
    peak_i = max(range(len(eq)), key=lambda i: eq[i])
    logs = [math.log(eq[i] / eq[i - 1]) for i in range(1, len(eq))]
    best365 = max(sum(logs[i:i + 365]) for i in range(0, len(logs) - 365))
    R["path"] = dict(longest_underwater_days=longest, current_dd=1 - eq[-1] / max(eq), peak_day=str(r.days[peak_i]),
                     days_since_peak=len(eq) - 1 - peak_i, share_best_365d=best365 / sum(logs))
    for w, key in ((365, "roll1y"), (1095, "roll3y")):
        a = [(eq[i + w] / eq[i]) ** (365 / w) - 1 for i in range(0, len(eq) - w, 7)]
        hb = [(bh.equity[i + w] / bh.equity[i]) ** (365 / w) - 1 for i in range(0, len(eq) - w, 7)]
        R[key] = dict(rule=(min(a), q(a, .5), max(a), sum(1 for x in a if x < 0) / len(a)),
                      bh=(min(hb), q(hb, .5), max(hb), sum(1 for x in hb if x < 0) / len(hb)), n=len(a))
    s = base["new|Yahoo|2018-01-01|1"]
    R["sharpe_se"] = math.sqrt((1 + 0.5 * s["sharpe"] ** 2) / s["years"])
    return r, r_old, bh


def sec_compare(y):
    """New weight vs old weight: coefficient sweep, sub-periods, random lookback sets."""
    C = {}
    C["coef"] = {str(c): (stats(run(y, "2018-01-01", 1, Params(down_penalty=c, **NB))), stats(run(y, "2022-01-01", 1, Params(down_penalty=c, **NB))),
                          stats(run(y, "2018-01-01", 0, Params(down_penalty=c, **NB))))
                 for c in (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.7, 1.0, 1.5)}
    C["sub"] = {}
    for s0, e0 in (("2018-01-01", "2020-12-31"), ("2021-01-01", "2023-12-31"), ("2024-01-01", END)):
        C["sub"][f"{s0[:4]}–{e0[:4]}"] = (stats(run(y, s0, 1, OLD, end=e0)), stats(run(y, s0, 1, P, end=e0)))
    random.seed(1)
    fill = shift_fill(y.close, 1)
    rn, ro, rn22 = [], [], []
    for _ in range(300):
        K = tuple(sorted(random.sample(range(10, 366), 7)))
        tn, to = targets2(y.close, Params(lookbacks=K, **NB))
        rn.append(stats(simulate(y.days, y.close, fill, "2018-01-01", END, P, FEE, SLIP, tgt=tn)))
        ro.append(stats(simulate(y.days, y.close, fill, "2018-01-01", END, P, FEE, SLIP, tgt=to)))
        rn22.append(stats(simulate(y.days, y.close, fill, "2022-01-01", END, P, FEE, SLIP, tgt=tn))["cagr"])
    bn, bo, bn22 = R["baseline"]["new|Yahoo|2018-01-01|1"], R["baseline"]["old|Yahoo|2018-01-01|1"], R["baseline"]["new|Yahoo|2022-01-01|1"]
    qs = (.05, .25, .5, .75, .95)
    C["random"] = dict(
        new_cagr=[x["cagr"] for x in rn], new_cagr_q=[q([x["cagr"] for x in rn], p_) for p_ in qs],
        new_dd_q=[q([x["maxdd"] for x in rn], p_) for p_ in qs],
        old_cagr_q=[q([x["cagr"] for x in ro], p_) for p_ in qs], old_dd_q=[q([x["maxdd"] for x in ro], p_) for p_ in qs],
        new_rank=sum(1 for x in rn if x["cagr"] < bn["cagr"]) / 300, new_dd_rank=sum(1 for x in rn if x["maxdd"] > bn["maxdd"]) / 300,
        old_rank=sum(1 for x in ro if x["cagr"] < bo["cagr"]) / 300,
        new_rank22=sum(1 for x in rn22 if x < bn22["cagr"]) / 300, new_median22=q(rn22, .5),
        d_cagr_q=[q([a["cagr"] - b_["cagr"] for a, b_ in zip(rn, ro)], p_) for p_ in (.05, .5, .95)],
        d_dd_q=[q([a["maxdd"] - b_["maxdd"] for a, b_ in zip(rn, ro)], p_) for p_ in (.05, .5, .95)],
        win_cagr=sum(1 for a, b_ in zip(rn, ro) if a["cagr"] > b_["cagr"]) / 300,
        win_dd=sum(1 for a, b_ in zip(rn, ro) if a["maxdd"] < b_["maxdd"]) / 300,
        win_sharpe=sum(1 for a, b_ in zip(rn, ro) if a["sharpe"] > b_["sharpe"]) / 300)
    R["cmp"] = C


def sec_sensitivity(y, hourly):
    S = {}
    sets = {"现用 {20,40,60,90,120,180,240}": LOOKBACKS, "评审备选 {30,60,90,180,365}": (30, 60, 90, 180, 365),
            "12 个回看期 {10…365}": (10, 20, 30, 60, 90, 120, 150, 180, 210, 240, 300, 365),
            "只用短期 {20,40,60}": (20, 40, 60), "只用长期 {120,180,240}": (120, 180, 240),
            "现用 ×0.75": (15, 30, 45, 68, 90, 135, 180), "现用 ×1.25": (25, 50, 75, 112, 150, 225, 300),
            "现用 ×1.5": (30, 60, 90, 135, 180, 270, 360)}
    for k in (20, 60, 120, 240, 365):
        sets[f"单个回看期 {k}"] = (k,)
    S["sets"] = {nm: (stats(run(y, "2018-01-01", 1, Params(lookbacks=K, **NB))),
                      stats(run(y, "2022-01-01", 1, Params(lookbacks=K, **NB)))) for nm, K in sets.items()}
    S["loo"] = {str(k): stats(run(y, "2018-01-01", 1, Params(lookbacks=tuple(x for x in LOOKBACKS if x != k), **NB))) for k in LOOKBACKS}
    S["vt"] = {str(v): (stats(run(y, "2018-01-01", 1, Params(vol_target=v, **NB))), stats(run(y, "2022-01-01", 1, Params(vol_target=v, **NB))))
               for v in (0.3, 0.4, 0.5, 0.6, None)}
    S["vw"] = {str(v): stats(run(y, "2018-01-01", 1, Params(vol_window=v, **NB))) for v in (20, 30, 45, 60, 90)}
    S["band"] = {str(v): (stats(run(y, "2018-01-01", 1, Params(band=v, **NB))), stats(run(y, "2022-01-01", 1, Params(band=v, **NB))))
                 for v in (0.0, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4)}
    S["cost"] = {f"{f}|{s}": stats(run(y, "2018-01-01", 1, fee=f, slip=s))
                 for f, s in ((0, 0), (0.001, 0), (0.001, 0.0005), (0.001, 0.002), (0.002, 0.0005), (0.0035, 0.0005), (0.002, 0.005), (0.0035, 0.01))}
    S["delay"] = {}
    for dh in (0, 1, 2, 4, 8, 12, 16, 24):
        cl, fl = hourly_fill_series(y, hourly, dh)
        S["delay"][str(dh)] = (stats(simulate(y.days, cl, fl, "2018-01-01", END, P, FEE, SLIP)),
                               stats(simulate(y.days, cl, fl, "2022-01-01", END, P, FEE, SLIP)))
    R["sens"] = S


def sec_breaker(y):
    out = {}
    for th in (None, 0.25, 0.30, 0.35, 0.40, 0.45):
        for s0 in ("2015-10-01", "2018-01-01"):
            r = simulate(y.days, y.close, shift_fill(y.close, 1), s0, END, Params(breaker_drawdown=th), FEE, SLIP, low=y.low)
            out[f"{th}|{s0}"] = dict(stats(r), events=[(str(e[0]), e[1]) for e in r.breaker_events])
    R["breaker"] = out


def sec_eth():
    e = load_daily("yahoo_eth.json")
    out = {"range": (str(e.days[0]), str(e.days[-1]))}
    for s0 in ("2018-08-01", "2022-01-01"):
        out[f"new|{s0}"] = stats(simulate(e.days, e.close, shift_fill(e.close, 1), s0, e.days[-1], P, FEE, SLIP))
        out[f"old|{s0}"] = stats(simulate(e.days, e.close, shift_fill(e.close, 1), s0, e.days[-1], OLD, FEE, SLIP))
        out[f"bh|{s0}"] = stats(simulate_static(e.days, e.close, s0, e.days[-1]))
    R["eth"] = out


def sec_plans(y):
    """110 USDT to start, +50 every 30 days, rolling 3-year windows."""
    Tn, To = targets2(y.close, P)
    fill = shift_fill(y.close, 1)
    res = {"new": [], "old": [], "dca": [], "half": []}
    d = date(2016, 1, 1)
    while d <= date(2023, 11, 1):
        e = d + timedelta(days=1095)
        kw = dict(initial=110.0, deposit_every=30, deposit_amount=50.0)
        res["new"].append(plan_stats(simulate(y.days, y.close, fill, d, e, P, FEE, SLIP, tgt=Tn, **kw)))
        res["old"].append(plan_stats(simulate(y.days, y.close, fill, d, e, P, FEE, SLIP, tgt=To, **kw)))
        res["dca"].append(plan_stats(simulate_static(y.days, y.close, d, e, 1.0, **kw)))
        res["half"].append(plan_stats(simulate_static(y.days, y.close, d, e, 0.5, rebalance_every=30, **kw)))
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    out = {"n": len(res["new"])}
    for k, v in res.items():
        f, w = [x["final_over_paid"] for x in v], [x["worst_interim"] for x in v]
        out[k] = dict(final_median=q(f, .5), final_min=min(f), final_max=max(f), worst_median=q(w, .5), worst_min=min(w))
    a, b_ = [x["final_over_paid"] for x in res["new"]], [x["final_over_paid"] for x in res["dca"]]
    out["rule_beats_dca"] = sum(1 for i in range(len(a)) if a[i] > b_[i]) / len(a)
    out["rule_beats_dca_2018"] = sum(1 for i in range(24, len(a)) if a[i] > b_[i]) / (len(a) - 24)
    R["plans"] = out


def sec_future(y, rule_run):
    """Two ways to look forward 3 years. (a) reshuffle the strategy's own daily
    returns (optimistic: keeps the realised timing success). (b) reshuffle BTC's
    price path in blocks and re-run both rules on each new path."""
    rs = [rule_run.equity[i] / rule_run.equity[i - 1] - 1 for i in range(1, len(rule_run.equity))]
    random.seed(7)
    cg = []
    for _ in range(4000):
        eq, n = 1.0, 0
        while n < 1095:
            s = random.randrange(0, len(rs) - 30)
            for r_ in rs[s:s + 30]:
                eq *= 1 + r_
                n += 1
                if n >= 1095:
                    break
        cg.append(eq ** (1 / 3) - 1)
    R["boot_returns"] = dict(median=q(cg, .5), p5=q(cg, .05), p_loss=sum(1 for c in cg if c < 0) / len(cg))

    i0 = y.idx("2018-01-01")
    src = [math.log(y.close[i] / y.close[i - 1]) for i in range(i0 + 1, len(y.close))]
    mu = sum(src) / len(src)
    H, WARM = 1095, 241
    days = [date(2000, 1, 1) + timedelta(days=i) for i in range(WARM + H)]

    def paths(M, mean_block, target_cagr, seed):
        rng = random.Random(seed)
        shift = 0.0 if target_cagr is None else math.log(1 + target_cagr) / 365 - mu
        new, old, bh = [], [], []
        for _ in range(M):
            lr, i = [], rng.randrange(len(src))
            while len(lr) < WARM + H - 1:                      # stationary bootstrap: geometric block lengths
                lr.append(src[i] + shift)
                i = rng.randrange(len(src)) if rng.random() < 1.0 / mean_block else (i + 1) % len(src)
            c = [100.0]
            for x in lr:
                c.append(c[-1] * math.exp(x))
            tn, to = targets2(c, P)
            f = shift_fill(c, 1)
            new.append(stats(simulate(days, c, f, days[WARM - 1], days[-1], P, FEE, SLIP, tgt=tn)))
            old.append(stats(simulate(days, c, f, days[WARM - 1], days[-1], P, FEE, SLIP, tgt=to)))
            bh.append(stats(simulate_static(days, c, days[WARM - 1], days[-1])))

        def summ(L):
            cg_, dd_ = [x["cagr"] for x in L], [x["maxdd"] for x in L]
            return dict(median=q(cg_, .5), p5=q(cg_, .05), p95=q(cg_, .95), loss=sum(1 for x in cg_ if x < 0) / M,
                        dd_median=q(dd_, .5), dd_p95=q(dd_, .95), dd_over45=sum(1 for x in dd_ if x >= 0.45) / M)
        return dict(new=summ(new), old=summ(old), bh=summ(bh), M=M,
                    new_better_cagr=sum(1 for a, b_ in zip(new, old) if a["cagr"] > b_["cagr"]) / M,
                    new_better_dd=sum(1 for a, b_ in zip(new, old) if a["maxdd"] < b_["maxdd"]) / M)
    fut = {"src_cagr": math.exp(mu * 365) - 1}
    for blk, drift in ((90, None), (90, 0.08), (90, 0.0), (30, 0.08), (250, 0.08)):
        fut[f"{blk}|{drift}"] = paths(300, blk, drift, 11)
    R["future"] = fut


# ---------------------------------------------------------------- charts ----
def charts(y, rule_run, old_run, bh_run):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    zh = None
    for name in ("Microsoft YaHei", "SimHei", "PingFang SC", "Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Micro Hei"):
        if any(f.name == name for f in font_manager.fontManager.ttflist):
            zh = name
            break
    L = (lambda c, e: c) if zh else (lambda c, e: e)
    BLUE, ORANGE, AQUA, YELLOW = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"   # validated categorical slots 1-4, fixed order
    INK, INK2, MUTED, GRID, AXIS, SURF = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#fcfcfb"
    plt.rcParams.update({
        "font.sans-serif": [zh or "DejaVu Sans", "DejaVu Sans"], "font.family": "sans-serif", "axes.unicode_minus": False,
        "figure.facecolor": SURF, "axes.facecolor": SURF, "savefig.facecolor": SURF, "axes.edgecolor": AXIS,
        "axes.labelcolor": INK2, "xtick.color": MUTED, "ytick.color": MUTED, "text.color": INK,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8, "axes.spines.top": False, "axes.spines.right": False,
        "axes.spines.left": False, "axes.titlesize": 12, "axes.titleweight": "bold", "axes.titlelocation": "left",
        "font.size": 10, "legend.frameon": False, "lines.linewidth": 2.0, "figure.dpi": 130,
    })
    IMG.mkdir(parents=True, exist_ok=True)
    half = simulate_static(y.days, y.close, "2018-01-01", END, 0.5, rebalance_every=30)
    d = rule_run.days
    series = [(L("新权重（实盘规则）", "New weight (live rule)"), rule_run.equity, BLUE), (L("买入持有", "Buy & hold"), bh_run.equity, ORANGE),
              (L("一半BTC一半USDT", "50/50 monthly"), half.equity, AQUA), (L("旧权重", "Old weight"), old_run.equity, YELLOW)]

    # 1. growth of 1 (log scale), end values labelled and nudged apart
    fig, ax = plt.subplots(figsize=(9.6, 4.6))
    for name, eq, col in series:
        ax.plot(d, eq, color=col, label=name)
    ax.set_yscale("log")
    ax.set_yticks([0.5, 1, 2, 4, 8]); ax.set_yticklabels(["0.5", "1", "2", "4", "8"])
    ends = sorted(((eq[-1], name, col) for name, eq, col in series))
    ypos, last = [], None
    for v, _, _ in ends:                                   # keep labels at least ~18% apart on the log axis
        yv = v if last is None else max(v, last * 1.18)
        ypos.append(yv); last = yv
    for (v, name, col), yv in zip(ends, ypos):
        ax.plot([d[-1]], [v], "o", ms=5, color=col, mec=SURF, mew=1.5, zorder=5)
        ax.annotate(f"{name} {v:.1f}×", (d[-1], yv), xytext=(8, 0), textcoords="offset points", va="center", color=INK2, fontsize=9)
    ax.set_title(L("1 元变成多少（2018-01 至 2026-11，对数坐标，已扣手续费和滑点，晚一天成交）",
                   "Growth of 1 (2018-01 to 2026-11, log scale, after costs, next-day fill)"))
    ax.legend(loc="upper left", ncol=4, fontsize=9)
    ax.set_xlim(d[0], d[-1] + timedelta(days=560))
    fig.tight_layout(); fig.savefig(IMG / "equity.png"); plt.close(fig)

    # 2. drawdowns
    fig, ax = plt.subplots(figsize=(9.6, 3.9))
    for name, eq, col in series:
        pk, dd = eq[0], []
        for e in eq:
            pk = max(pk, e); dd.append((e / pk - 1) * 100)
        ax.plot(d, dd, color=col, label=f"{name}（{L('最深', 'worst')} {min(dd):.0f}%）", linewidth=1.5)
    ax.plot([d[0], d[-1]], [-45, -45], color=MUTED, linewidth=1, linestyle=(0, (4, 3)))
    ax.annotate(L("熔断线 -45%", "breaker -45%"), (d[-1], -45), xytext=(8, 0), textcoords="offset points", va="center",
                color=INK2, fontsize=9)
    ax.set_title(L("从最高点回落了多少（%）", "Drawdown from peak (%)"))
    ax.set_ylim(-92, 2); ax.legend(loc="lower left", ncol=4, fontsize=8.5)
    ax.set_xlim(d[0], d[-1] + timedelta(days=330))
    fig.tight_layout(); fig.savefig(IMG / "drawdown.png"); plt.close(fig)

    # 3. BTC weight held by the live rule
    fig, ax = plt.subplots(figsize=(9.6, 3.0))
    ax.fill_between(d, [w * 100 for w in rule_run.weight], step="post", color=BLUE, alpha=0.25, linewidth=0)
    ax.plot(d, [w * 100 for w in rule_run.weight], color=BLUE, linewidth=1.2, drawstyle="steps-post")
    ax.set_ylim(0, 105); ax.set_yticks([0, 25, 50, 75, 100])
    avg = sum(rule_run.weight) / len(rule_run.weight) * 100
    ax.set_title(L(f"新权重持有的 BTC 仓位（%），平均 {avg:.0f}%", f"BTC weight held by the live rule (%), average {avg:.0f}%"))
    fig.tight_layout(); fig.savefig(IMG / "weight.png"); plt.close(fig)

    # 4. is the chosen lookback set lucky? histogram of 300 random sets (new weight)
    rnd = [x * 100 for x in R["cmp"]["random"]["new_cagr"]]
    base = R["baseline"]["new|Yahoo|2018-01-01|1"]["cagr"] * 100
    fig, ax = plt.subplots(figsize=(9.6, 3.6))
    lo, hi = math.floor(min(rnd)), math.ceil(max(max(rnd), base)) + 1
    ax.hist(rnd, bins=[lo + i for i in range(hi - lo + 1)], color=BLUE, alpha=0.85, rwidth=0.9)
    top = ax.get_ylim()[1]
    ax.set_ylim(0, top * 1.2)
    ax.set_axisbelow(True)
    top *= 1.17                                            # labels sit above the bars
    ax.axvline(base, color=INK, linewidth=1.5)
    ax.annotate(L(f"现用组合 {base:.1f}%", f"chosen set {base:.1f}%"), (base, top * 0.92), xytext=(6, 0),
                textcoords="offset points", color=INK, fontsize=9)
    med = q(rnd, .5)
    ax.axvline(med, color=MUTED, linewidth=1.2, linestyle=(0, (4, 3)))
    ax.annotate(L(f"随机组合中位数 {med:.1f}%", f"random median {med:.1f}%"), (med, top * 0.92), xytext=(-6, 0),
                textcoords="offset points", ha="right", color=INK2, fontsize=9)
    ax.set_title(L("随机挑 7 个回看期（300 组，新权重）的年化收益分布，2018 年至今", "CAGR of 300 random sets of 7 lookbacks (new weight), 2018 to date"))
    ax.set_xlabel(L("年化收益 %", "CAGR %")); ax.set_ylabel(L("组数", "count")); ax.grid(axis="x", visible=False)
    fig.tight_layout(); fig.savefig(IMG / "random_sets.png"); plt.close(fig)


# ---------------------------------------------------------------- report ----
def write_report():
    B, K, S, C = R["baseline"], R["bench"], R["sens"], R["cmp"]
    n0, n1, nh = B["new|Yahoo|2018-01-01|0"], B["new|Yahoo|2018-01-01|1"], B["new|hourly1|2018-01-01"]
    o0, o1, oh = B["old|Yahoo|2018-01-01|0"], B["old|Yahoo|2018-01-01|1"], B["old|hourly1|2018-01-01"]
    n22, o22 = B["new|Yahoo|2022-01-01|1"], B["old|Yahoo|2022-01-01|1"]
    rv, fut, pl, rnd = R.get("review", {}), R["future"], R["plans"], C["random"]

    def rvc(s0, lag, fee=0.0005):
        v = rv.get(f"{s0}|{lag}|{fee}")
        return f"{pc(v[0])} / {pc(v[1], 0)}" if v else "未能运行"
    f8, f0, fs = fut["90|0.08"], fut["90|0.0"], fut["90|None"]
    news18 = [B[k] for k in B if k.startswith("new|") and "|2018-01-01" in k]
    olds18 = [B[k] for k in B if k.startswith("old|") and "|2018-01-01" in k]
    dd_new = (min(x["maxdd"] for x in news18), max(x["maxdd"] for x in news18))
    dd_old = (min(x["maxdd"] for x in olds18), max(x["maxdd"] for x in olds18))
    d_cagr = [B[k]["cagr"] - B["old" + k[3:]]["cagr"] for k in B if k.startswith("new|") and "|2018-01-01" in k]
    d_dd = [B["old" + k[3:]]["maxdd"] - B[k]["maxdd"] for k in B if k.startswith("new|") and "|2018-01-01" in k]
    d_cagr0 = [B[k]["cagr"] - B["old" + k[3:]]["cagr"] for k in B if k.startswith("new|") and (k.endswith("|2018-01-01|0") or k == "new|hourly0|2018-01-01")]
    all_dd95 = [fut[k]["new"]["dd_p95"] for k in fut if k != "src_cagr"]
    uw_years = R["path"]["longest_underwater_days"] / 365.0
    L = []
    A = L.append
    A("# BTC 现货趋势规则：独立回测复核报告")
    A("")
    A(f"生成日期 2026-11-21｜数据截至 {END}（UTC 日线）｜由 `python backtest/run_all.py` 自动生成，所有数字都能重跑复现")
    A("")
    A("> 这是研究记录，不是投资建议。回测是对过去的统计，未来可能完全不同。")
    A("")
    A("两个名词：**旧权重** = 评审最初的提案，仓位 = 上涨的回看期个数 ÷ 7 × V。**新权重** = 2026-11-21 起默认采用、脚本实盘使用的版本，"
      "仓位 = max(0, 上涨个数 − 0.3 × 下跌个数) ÷ 7 × V（上涨 0–7 个时分别是 0、0、0.07、0.26、0.44、0.63、0.81、1，再乘 V）。其余完全相同。")
    A("")
    A("## 一页结论")
    A("")
    A(f"1. **评审报的旧权重数字：收益对，回撤略偏乐观。** 用评审自己的代码重跑，结果和它一样（2018 年至今年化 {rvc('2018-01-01', 0).split(' / ')[0]}（收盘成交）/ {rvc('2018-01-01', 1).split(' / ')[0]}（晚一天成交），最大回撤约 35%）。"
      f"按真实账户方式重写后（现货手续费 0.1% + 滑点 0.05%，仓位随价格漂移）：年化 {pc(o0['cagr'])} / {pc(o1['cagr'])}，最大回撤 **{pc(o1['maxdd'], 0)}**，比评审说的多 3 个点左右。")
    A(f"2. **新权重：「回撤更小」能复现，「收益更高」复现不出来。** 提案报告说年化 23.1% → 27.3%、最大回撤 37.3% → 27.2%。我的结果（2018 年至今）：")
    A(f"   - 最大回撤：旧 {pc(dd_old[0], 0)}–{pc(dd_old[1], 0)} → 新 **{pc(dd_new[0], 0)}–{pc(dd_new[1], 0)}**（三个数据源、各种成交时点全都下降，少 {min(d_dd)*100:.0f}–{max(d_dd)*100:.0f} 个点）。这一半成立。")
    A(f"   - 年化：收盘成交 {pc(o0['cagr'])} → {pc(n0['cagr'])}；收盘后 1 小时成交 {pc(oh['cagr'])} → {pc(nh['cagr'])}；晚一天成交 {pc(o1['cagr'])} → {pc(n1['cagr'])}。"
      f"各种数据源和成交时点下，新减旧在 {pp(min(d_cagr))} 到 {pp(max(d_cagr))} 个点之间，**基本持平**，不是提高 4 个点。")
    A(f"   - 换成随机的 7 个回看期（300 组）：新权重收益更高的只占 {pc(rnd['win_cagr'], 0)}（中位数 {pp(rnd['d_cagr_q'][1])} 个点），回撤更小的占 {pc(rnd['win_dd'], 0)}（中位数 {pp(rnd['d_dd_q'][1])} 个点）。"
      f"在 300 条模拟的未来路径上：年化中位数 {pc(f8['old']['median'], 0)} → {pc(f8['new']['median'], 0)}，回撤更小的路径占 {pc(f8['new_better_dd'], 0)}。")
    A(f"   - **结论**：新权重本质是「平均仓位从 {pc(o1['exposure'], 0)} 降到 {pc(n1['exposure'], 0)}、意见不统一时少拿」，换来回撤小几个点、收益大致不变。这和「少亏」的目标一致，可以用；"
      f"但不要指望它多赚，现用回看期上 {min(d_dd)*100:.0f}–{max(d_dd)*100:.0f} 个点的回撤改善也偏幸运（换成随机回看期，典型只有 {abs(rnd['d_dd_q'][1])*100:.0f} 个点左右）。已设为默认实盘规则，旧权重作为影子账并行记录 6–12 个月。")
    A(f"3. **历史数字不能当作对未来的预期。** ① 现用的 7 个回看期在随机 300 组里年化排第 {rnd['new_rank']*100:.0f} 百分位（随机组合中位数只有 {pc(rnd['new_cagr_q'][2])} / 回撤 {pc(rnd['new_dd_q'][2], 0)}），带有运气；"
      f"② 2018 年以来的利润有 {pc(R['path']['share_best_365d'], 0)} 来自最好的那 12 个月；③ 2022 年以来年化只有 {pc(n22['cagr'])}。")
    A(f"4. **合理预期：年化大约 10–20%，任意 3 年亏钱的可能约 1/4 到 1/3，账面回撤要按 -45% 到 -50% 做准备，可能连续两三年不创新高。** "
      f"把比特币历史价格按段打乱重排成 300 条「未来 3 年」的路径再跑新权重：若比特币今后年化 8%，规则年化中位数 {pc(f8['new']['median'], 0)}、3 年亏钱概率 {pc(f8['new']['loss'], 0)}；"
      f"若比特币不涨，中位数 {pc(f0['new']['median'], 0)}、亏钱概率 {pc(f0['new']['loss'], 0)}；趋势被打得更碎时（每段 30 天）中位数只有 {pc(fut['30|0.08']['new']['median'], 0)}、亏钱概率 {pc(fut['30|0.08']['new']['loss'], 0)}。"
      f"最差 5% 的路径回撤 {pc(min(all_dd95), 0)}–{pc(max(all_dd95), 0)}。历史上最长连续 {R['path']['longest_underwater_days']} 天（约 {uw_years:.1f} 年）没创新高；现在距 {R['path']['peak_day']} 的高点已回落 {pc(R['path']['current_dd'], 0)}。")
    A(f"5. **这套规则的价值是「少亏」，不是「多赚」。** 同样 110U 起步、每 30 天存 50U、持有 3 年（{pl['n']} 个不同起点）：「存入就买、一直持有」期末是投入的 {pl['dca']['final_median']:.2f} 倍（中位数），新权重 {pl['new']['final_median']:.2f} 倍；"
      f"但过程中最深的账面浮亏，持有是 {pc(pl['dca']['worst_min'], 0)}，新权重是 {pc(pl['new']['worst_min'], 0)}。规则只在 {pc(pl['rule_beats_dca'], 0)} 的起点上最终比定投赚得多。"
      "如果能扛住账面 -60% 不割肉，每月定投并持有更简单，历史上钱也更多；选这套规则是为了跌的时候拿得住。")
    A("6. **其余参数不用改。** 回看期、波动率目标（30%/40%/50%）、调仓门槛（0.1–0.3）、扣减系数（0.2–1.0）、成交延迟（0–24 小时）左右挪动，结果都在同一范围；对手续费和下单时间不敏感。")
    A(f"7. **-45% 熔断在历史上从未触发**（新权重历史最大回撤 {pc(n1['maxdd'], 0)}），它是「规则可能失效了」的警报，不是日常风控；收紧会伤害收益，不要收紧。")
    A("8. **没有验证的**：未来趋势效应是否延续；USDT 脱锚、欧易平台风险；实盘成交（没有 API key，下单流程只用模拟对象测过）。")
    A("")
    A("## 1. 数据与方法")
    A("")
    D = R["data"]
    A("| 数据源 | 范围 | 天数 | 缺口 | 用途 |")
    A("|---|---|---|---|---|")
    A(f"| Yahoo BTC-USD 日线 | {D['yahoo'][0]} ~ {D['yahoo'][1]} | {D['yahoo'][2]} | {D['yahoo'][3]} | 主回测（和评审同源，便于对比）|")
    A(f"| Bitstamp BTC/USD 日线 | {D['bitstamp'][0]} ~ {D['bitstamp'][1]} | {D['bitstamp'][2]} | {D['bitstamp'][3]} | 第二数据源交叉核对 |")
    A(f"| OKX BTC-USDT 日线 | {D['okx'][0]} ~ {D['okx'][1]} | {D['okx'][2]} | {D['okx'][3]} | 实际交易的市场（2018-01-11 之前用 Bitstamp 补历史）|")
    A("| Binance BTCUSDT 小时线 | 2017-08 起 | — | — | 测试「收盘后晚 N 小时成交」|")
    A("")
    A(f"- Yahoo 缺 {', '.join(D['patched']) or '无'} 这一天，用 Bitstamp 补上（评审当时用的是 Kraken）。")
    A(f"- 各源收盘价的差异：OKX 对 Bitstamp（2020 年以来）中位数 {pc(D['okx_vs_bitstamp_2020']['median'], 3)}、99% 分位 {pc(D['okx_vs_bitstamp_2020']['p99'], 2)}、最大 {pc(D['okx_vs_bitstamp_2020']['max'], 2)}；"
      f"Yahoo 对 OKX 中位数 {pc(D['yahoo_vs_okx']['median'], 3)}、最大 {pc(D['yahoo_vs_okx']['max'], 2)}（Yahoo 是聚合价，个别日子偏差大，而且会缺最近一天，所以**实盘脚本不用 Yahoo**，用 OKX 并以 Binance/Kraken 等交叉核对）。")
    A("- 回测方式：模拟真实账户里的 USDT 和 BTC 数量；每天 UTC 收盘后用**和实盘脚本同一份代码**算信号、判断是否调仓；买卖按成交价加减滑点并扣手续费；仓位随价格自然漂移。")
    A(f"- 成本假设：手续费 {pc(FEE, 2)}（欧易普通用户现货吃单费率，官方 2026-07 公告；部分地区是 0.2%/0.35%，见 5.5 节）+ 滑点 {pc(SLIP, 2)}。")
    A("- 「晚一天成交」= 信号出来后等到下一个收盘价才成交，最保守；实盘是收盘后约 22 分钟成交，介于「收盘成交」和「1 小时后成交」之间。")
    A("- 除第 9 节外，表里都没有开熔断（历史上 -45% 从未触发，开不开结果一样）。")
    A("")
    A("## 2. 复核评审的数字（旧权重）")
    A("")
    A("| 2018-01 至 2026-11-20 | 年化 / 最大回撤（收盘成交）| 年化 / 最大回撤（晚一天成交）|")
    A("|---|---|---|")
    A("| 评审报告的数字 | 25.0% / 35% | 22.6% / 35% |")
    A(f"| 用评审的代码在新数据上重跑（手续费 0.05%）| {rvc('2018-01-01', 0)} | {rvc('2018-01-01', 1)} |")
    A(f"| 同上，手续费改为现货的 0.1% | {rvc('2018-01-01', 0, 0.001)} | {rvc('2018-01-01', 1, 0.001)} |")
    A(f"| **本报告的账户级回测，旧权重（0.1% + 滑点 0.05%）** | **{cell(o0)}** | **{cell(o1)}** |")
    A("")
    A("差别来自三处：评审用的是合约的 0.05% 手续费（现货是 0.1%）；它假设仓位每天免费回到目标比例、调仓门槛是拿「新目标」和「上次目标」比，"
      "而真实账户里仓位会随价格漂移、门槛是拿目标和**实际仓位**比（脚本就是这么做的）；它的波动率用普通收益率而不是对数收益率。收益几乎不受影响，回撤多了约 3 个点。")
    A("")
    A("## 3. 新权重对旧权重")
    A("")
    A("每格是 年化 / 最大回撤，2018-01 至 2026-11-20，0.1% + 0.05% 成本：")
    A("")
    A("| 数据 / 成交时点 | 旧权重 | 新权重 | 年化变化（点）| 回撤变化（点）|")
    A("|---|---|---|---|---|")
    rows = [("Yahoo，收盘成交", "Yahoo|2018-01-01|0"), ("Yahoo，晚一天", "Yahoo|2018-01-01|1"), ("Bitstamp，收盘成交", "Bitstamp|2018-01-01|0"),
            ("Bitstamp，晚一天", "Bitstamp|2018-01-01|1"), ("OKX，收盘成交", "OKX|2018-01-01|0"), ("OKX，晚一天", "OKX|2018-01-01|1"),
            ("Binance 小时线，收盘后 0 小时", "hourly0|2018-01-01"), ("Binance 小时线，收盘后 1 小时", "hourly1|2018-01-01"), ("Binance 小时线，收盘后 24 小时", "hourly24|2018-01-01")]
    for name, key in rows:
        a, b_ = B[f"old|{key}"], B[f"new|{key}"]
        A(f"| {name} | {cell(a)} | {cell(b_)} | {pp(b_['cagr'] - a['cagr'])} | {pp(b_['maxdd'] - a['maxdd'], 0)} |")
    A(f"| 提案报告的数字 | 23.1% / 37% | 27.3% / 27% | +4.2 | -10 |")
    A(f"| 提案报告，晚 24 小时 | 22.8% / 38% | 23.5% / 29% | +0.7 | -9 |")
    A("")
    A(f"- 晚一天成交的数字和提案报告吻合；收盘成交的「+4.2 个点」在我这里是 {pp(min(d_cagr0))} 到 {pp(max(d_cagr0))}（四种数据），没有复现。")
    A(f"- 夏普比率 {o1['sharpe']:.2f} → {n1['sharpe']:.2f}（统计误差约 ±{R['sharpe_se']:.2f}，这点差别在误差之内）；平均仓位 {pc(o1['exposure'], 0)} → {pc(n1['exposure'], 0)}；每年调仓 {o1['trades_per_year']:.0f} → {n1['trades_per_year']:.0f} 次。")
    A(f"- 2022 年起（晚一天成交）：旧 {cell(o22)} → 新 {cell(n22)}。")
    A("")
    A("分段看（Yahoo，晚一天成交）：")
    A("")
    A("| 区间 | 旧权重 | 新权重 |")
    A("|---|---|---|")
    for k, (a, b_) in C["sub"].items():
        A(f"| {k} | {cell(a)} | {cell(b_)} |")
    A("")
    A("扣减系数（现用 0.3）挪一挪，Yahoo，晚一天成交：")
    A("")
    A("| 系数 | " + " | ".join(k + ("（旧）" if k == "0.0" else "（现用）" if k == "0.3" else "") for k in C["coef"]) + " |")
    A("|---|" + "---|" * len(C["coef"]))
    A("| 2018 年起 | " + " | ".join(cell(v[0]) for v in C["coef"].values()) + " |")
    A("| 2022 年起 | " + " | ".join(cell(v[1]) for v in C["coef"].values()) + " |")
    A("| 平均仓位 | " + " | ".join(pc(v[0]["exposure"], 0) for v in C["coef"].values()) + " |")
    A("")
    A("- 系数越大仓位越低、回撤越小，收益缓慢下降：它是一个「降仓位」的旋钮，0.2–1.0 之间没有哪个值特别突出，0.3 不是挑出来的尖峰。")
    A("")
    A("换成随机的 7 个回看期（10–365 天里随机挑，300 组），每组都分别用新旧权重跑一遍（2018 年起，晚一天成交）：")
    A("")
    A("| | 5% 分位 | 中位数 | 95% 分位 |")
    A("|---|---|---|---|")
    A(f"| 年化：新 − 旧（点）| {pp(rnd['d_cagr_q'][0])} | {pp(rnd['d_cagr_q'][1])} | {pp(rnd['d_cagr_q'][2])} |")
    A(f"| 最大回撤：新 − 旧（点，负数 = 回撤更小）| {pp(rnd['d_dd_q'][0])} | {pp(rnd['d_dd_q'][1])} | {pp(rnd['d_dd_q'][2])} |")
    A("")
    A(f"- 新权重年化更高的组占 {pc(rnd['win_cagr'], 0)}，回撤更小的占 {pc(rnd['win_dd'], 0)}，夏普更高的占 {pc(rnd['win_sharpe'], 0)}。")
    A(f"- 现用回看期在新权重下：年化排在随机组合的第 {rnd['new_rank']*100:.0f} 百分位，回撤比 {pc(rnd['new_dd_rank'], 0)} 的随机组合都小（旧权重下年化排第 {rnd['old_rank']*100:.0f} 百分位）。也就是说「现用回看期 + 0.3」这个组合在这段历史上尤其顺，典型的改善要小得多。")
    A(f"- 以太坊（参数不动）：2018-08 起 旧 {cell(R['eth']['old|2018-08-01'])} → 新 {cell(R['eth']['new|2018-08-01'])}；2022 年起 旧 {cell(R['eth']['old|2022-01-01'])} → 新 {cell(R['eth']['new|2022-01-01'])}。方向一致（以太坊与比特币高度相关，不算独立证据）。")
    A("- 模拟的未来路径上的对比见第 7 节。")
    A("")
    A("**复核结论**：新权重在所有测试里回撤都不比旧权重差，多数更好；收益大致不变（略低的时候多于略高的时候）。提案里「年化提高 4 个点」不成立，「回撤降 10 个点」只在现用回看期上成立。"
      "它是一个温和的降风险改动，不构成反对采用的理由，但预期应当是「回撤小几个点」，不是「又多赚又少亏」。")
    A("")
    A("## 4. 基准结果（新权重）")
    A("")
    A("年化 / 最大回撤（按日收盘计）：")
    A("")
    A("| 区间 | 数据 | 收盘成交 | 晚一天成交 | 买入持有 | 一半BTC一半USDT |")
    A("|---|---|---|---|---|---|")
    for s0 in PERIODS:
        for nm in ("Yahoo", "Bitstamp", "OKX"):
            if f"new|{nm}|{s0}|0" not in B:
                continue
            bh = cell(K[f"bh|{s0}"]) if nm == "Yahoo" else ""
            hf = cell(K[f"half|{s0}"]) if nm == "Yahoo" else ""
            A(f"| {s0[:7]} 起 | {nm} | {cell(B[f'new|{nm}|{s0}|0'])} | {cell(B[f'new|{nm}|{s0}|1'])} | {bh} | {hf} |")
    A("")
    A(f"- 收盘后 1 小时成交（Binance 小时线）：2018 年起 {cell(nh)}，2022 年起 {cell(B['new|hourly1|2022-01-01'])}。")
    A(f"- 2018 年起（晚一天成交）：夏普比率 {n1['sharpe']:.2f}（统计误差约 ±{R['sharpe_se']:.2f}，也就是说「真实值是 0.6 还是 1.3」这段历史分不清）；年化波动 {pc(n1['vol'], 0)}；"
      f"平均仓位 {pc(n1['exposure'], 0)}，有持仓的天数占 {pc(n1['in_market'], 0)}；每年调仓约 {n1['trades_per_year']:.0f} 次，年换手 {n1['turnover']:.1f} 倍；按盘中最低价算的最大回撤 {pc(n1['maxdd_low'], 0)}。")
    A("- 2015–2017 的数字来自比特币从几百美元涨到两万的阶段，不应外推。")
    A("")
    A("![净值](img/equity.png)")
    A("")
    A("![回撤](img/drawdown.png)")
    A("")
    A("![仓位](img/weight.png)")
    A("")
    A("分年收益（Yahoo，晚一天成交）：")
    A("")
    A("| 年份 | " + " | ".join(R["yearly"].keys()) + " |")
    A("|---|" + "---|" * len(R["yearly"]))
    A("| 新权重 | " + " | ".join(pc(v[0], 0) for v in R["yearly"].values()) + " |")
    A("| 旧权重 | " + " | ".join(pc(v[1], 0) for v in R["yearly"].values()) + " |")
    A("| 买入持有 | " + " | ".join(pc(v[2], 0) for v in R["yearly"].values()) + " |")
    A("")
    r1, r3 = R["roll1y"], R["roll3y"]
    A("任意起点持有一段时间的年化收益（新权重，2018 年起，每周取一个起点）：")
    A("")
    A("| | 最差 | 中位数 | 最好 | 亏钱的起点占比 |")
    A("|---|---|---|---|---|")
    A(f"| 新权重，持有 1 年 | {pc(r1['rule'][0], 0)} | {pc(r1['rule'][1], 0)} | {pc(r1['rule'][2], 0)} | {pc(r1['rule'][3], 0)} |")
    A(f"| 买入持有，持有 1 年 | {pc(r1['bh'][0], 0)} | {pc(r1['bh'][1], 0)} | {pc(r1['bh'][2], 0)} | {pc(r1['bh'][3], 0)} |")
    A(f"| 新权重，持有 3 年 | {pc(r3['rule'][0], 0)} | {pc(r3['rule'][1], 0)} | {pc(r3['rule'][2], 0)} | {pc(r3['rule'][3], 0)} |")
    A(f"| 买入持有，持有 3 年 | {pc(r3['bh'][0], 0)} | {pc(r3['bh'][1], 0)} | {pc(r3['bh'][2], 0)} | {pc(r3['bh'][3], 0)} |")
    A("")
    A("注意：这些 3 年窗口互相重叠，其实只覆盖约 3 轮牛熊，「历史上 3 年几乎没亏过」的说服力有限，见第 7 节的模拟。")
    A("")
    A("## 5. 稳健性：参数挪一挪结果变不变（新权重）")
    A("")
    A("全部用 Yahoo 数据、晚一天成交、0.1% + 0.05% 成本；每格是 年化 / 最大回撤。")
    A("")
    A("### 5.1 回看期")
    A("")
    A("| 回看期组合 | 2018 年起 | 2022 年起 |")
    A("|---|---|---|")
    for nm, (a, b_) in S["sets"].items():
        A(f"| {nm} | {cell(a)} | {cell(b_)} |")
    loo = list(S["loo"].values())
    singles = [v[0] for k, v in S["sets"].items() if k.startswith("单个")]
    A("")
    A(f"- 从现用 7 个里去掉任意 1 个：年化 {pc(min(v['cagr'] for v in loo))} ~ {pc(max(v['cagr'] for v in loo))}，最大回撤 {pc(min(v['maxdd'] for v in loo), 0)} ~ {pc(max(v['maxdd'] for v in loo), 0)}。")
    A(f"- 随机挑 7 个回看期 300 次：年化的 5%/25%/50%/75%/95% 分位是 {' / '.join(pc(x) for x in rnd['new_cagr_q'])}，最大回撤分位 {' / '.join(pc(x, 0) for x in rnd['new_dd_q'])}。"
      f"现用组合的年化排在第 **{rnd['new_rank']*100:.0f}** 百分位（2022 年起排第 {rnd['new_rank22']*100:.0f} 百分位，随机中位数 {pc(rnd['new_median22'])}）。")
    A(f"- 结论：多个回看期合在一起比单个稳（单个回看期的回撤是 {pc(min(v['maxdd'] for v in singles), 0)}–{pc(max(v['maxdd'] for v in singles), 0)}）；现用这组在这段历史上偏幸运，"
      f"比随机组合的中位数高约 {(n1['cagr'] - rnd['new_cagr_q'][2]) * 100:.0f} 个点，对未来应按中位数（约 {pc(rnd['new_cagr_q'][2], 0)}）甚至更低来想。")
    A("")
    A("![随机回看期](img/random_sets.png)")
    A("")
    A("### 5.2 波动率目标与窗口")
    A("")
    A("| 波动率目标 | 2018 年起 | 2022 年起 | 平均仓位 |")
    A("|---|---|---|---|")
    for k, (a, b_) in S["vt"].items():
        A(f"| {'不用（只看趋势）' if k == 'None' else pc(float(k), 0)}{'（现用）' if k == '0.4' else ''} | {cell(a)} | {cell(b_)} | {pc(a['exposure'], 0)} |")
    A("")
    A("| 波动率窗口（天）| " + " | ".join(k + ("（现用）" if k == "30" else "") for k in S["vw"]) + " |")
    A("|---|" + "---|" * len(S["vw"]))
    A("| 2018 年起 | " + " | ".join(cell(v) for v in S["vw"].values()) + " |")
    A("")
    A(f"- 波动率目标的作用是**降仓位、降回撤**（不用它回撤是 {pc(S['vt']['None'][0]['maxdd'], 0)}），不是提高收益；30%/40%/50% 是「收益换回撤」的旋钮，没有哪个明显更优。")
    vws = [v["cagr"] for v in S["vw"].values()]
    A(f"- 波动率窗口 20–90 天，年化在 {pc(min(vws), 0)}–{pc(max(vws), 0)} 之间。45–60 天在这段历史上数字更好看，但在旧权重下各窗口都在 20%–24% 之间、没有这个差别，属于噪声，不据此改参数。")
    A("")
    A("### 5.3 调仓门槛")
    A("")
    A("| 门槛 | 2018 年起 | 2022 年起 | 每年调仓次数 |")
    A("|---|---|---|---|")
    for k, (a, b_) in S["band"].items():
        A(f"| {k}{'（现用）' if k == '0.2' else ''} | {cell(a)} | {cell(b_)} | {a['trades_per_year']:.0f} |")
    bands = [S["band"][k][0]["cagr"] for k in ("0.1", "0.15", "0.2", "0.25", "0.3")]
    A("")
    A(f"- 门槛从 0.1 到 0.3，年化在 {pc(min(bands), 0)}–{pc(max(bands), 0)} 之间无规律地跳，属于噪声。它的实际好处是把每年 {S['band']['0.0'][0]['trades_per_year']:.0f} 次调仓降到约 {S['band']['0.2'][0]['trades_per_year']:.0f} 次，适合小账户。")
    A("")
    A("### 5.4 成交延迟（收盘后晚几个小时才成交）")
    A("")
    A("| 延迟 | " + " | ".join(f"{k} 小时" for k in S["delay"]) + " |")
    A("|---|" + "---|" * len(S["delay"]))
    A("| 2018 年起 | " + " | ".join(cell(v[0]) for v in S["delay"].values()) + " |")
    A("| 2022 年起 | " + " | ".join(cell(v[1]) for v in S["delay"].values()) + " |")
    early = [v for k, v in S["delay"].items() if int(k) <= 12]
    late = [v for k, v in S["delay"].items() if int(k) >= 16]
    A("")
    A(f"- 收盘后 0–12 小时内成交，差别很小（2018 年起年化 {pc(min(v[0]['cagr'] for v in early), 0)}–{pc(max(v[0]['cagr'] for v in early), 0)}，2022 年起 {pc(min(v[1]['cagr'] for v in early), 0)}–{pc(max(v[1]['cagr'] for v in early), 0)}）。"
      f"拖到 16–24 小时会差一些（2018 年起 {pc(min(v[0]['cagr'] for v in late), 0)}–{pc(max(v[0]['cagr'] for v in late), 0)}，2022 年起 {pc(min(v[1]['cagr'] for v in late), 0)}–{pc(max(v[1]['cagr'] for v in late), 0)}）。")
    A("- 所以：哪天因为故障晚跑几个小时不要紧，不必手动干预；但不要习惯性地拖到第二天。本报告其他表格用的「晚一天成交」是偏保守的假设。")
    A("")
    A("### 5.5 手续费与滑点")
    A("")
    A("| 手续费 + 滑点（每笔）| 2018 年起 |")
    A("|---|---|")
    for k, v in S["cost"].items():
        f, s = k.split("|")
        A(f"| {pc(float(f), 2)} + {pc(float(s), 2)} | {cell(v)} |")
    A("")
    A(f"- 每年只调仓约 {n1['trades_per_year']:.0f} 次，成本影响小：手续费从 0.1% 变成 0.35%，年化掉 {(S['cost']['0.001|0.0005']['cagr'] - S['cost']['0.0035|0.0005']['cagr']) * 100:.1f} 个点；"
      f"0.35% 再加 1% 滑点，掉 {(S['cost']['0.001|0.0005']['cagr'] - S['cost']['0.0035|0.01']['cagr']) * 100:.1f} 个点。**请在欧易 App 里确认自己的实际费率**（部分地区普通用户是 0.2% 或 0.35%），不是 0.1% 就在 `.env` 里改 `SPOT_FEE`。")
    A("")
    A("## 6. 它到底靠什么赚钱")
    A("")
    A("| Yahoo，晚一天成交 | 2018 年起 | 2022 年起 |")
    A("|---|---|---|")
    for key, name in (("", "新权重（趋势 × 波动率目标）"), ("old", "旧权重"), ("E_only", "只用趋势，不用波动率目标"), ("V_only", "只用波动率目标，不看趋势"),
                      ("same_exposure", "固定仓位（和新权重的平均仓位相同，每月调平）"), ("half", "一半BTC一半USDT，每月调平"), ("bh", "买入持有")):
        a = n1 if not key else o1 if key == "old" else K[f"{key}|2018-01-01"]
        b_ = n22 if not key else o22 if key == "old" else K[f"{key}|2022-01-01"]
        A(f"| {name} | {cell(a)}（夏普 {a['sharpe']:.2f}）| {cell(b_)}（夏普 {b_['sharpe']:.2f}）|")
    se = K["same_exposure|2018-01-01"]
    A("")
    A(f"- 收益来自**趋势判断**（跌势里空仓）：和「同样平均仓位但不择时」相比，年化高 {(n1['cagr'] - se['cagr']) * 100:.0f} 个点、回撤少 {(se['maxdd'] - n1['maxdd']) * 100:.0f} 个点。"
      f"波动率目标单独用没有价值，它负责把回撤从 {pc(K['E_only|2018-01-01']['maxdd'], 0)} 压到 {pc(n1['maxdd'], 0)}。")
    A(f"- 样本外检查：同样的参数直接用在以太坊上（{R['eth']['range'][0]} 起，没有做任何调整）：2018-08 起 {cell(R['eth']['new|2018-08-01'])}，买入持有 {cell(R['eth']['bh|2018-08-01'])}；"
      f"2022 年起 {cell(R['eth']['new|2022-01-01'])}，买入持有 {cell(R['eth']['bh|2022-01-01'])}。以太坊和比特币高度相关，这不算真正独立的证据。")
    A("")
    A("## 7. 对未来 3 年的预期")
    A("")
    A("两种模拟方法，结论差别很大：")
    A("")
    br = R["boot_returns"]
    A(f"- **方法 A（偏乐观）**：把规则自己过去每天的盈亏打乱重排。3 年年化中位数 {pc(br['median'], 0)}，亏钱概率 {pc(br['p_loss'], 0)}。它默认规则今后择时和过去一样准，所以偏乐观；早先「3 年亏钱概率约 12%」的说法就是这么来的，不应采用。")
    A("- **方法 B（更诚实）**：把**比特币的价格走势**按段打乱重排成新的 3 年路径（保留一部分趋势性），并假设比特币今后的涨幅没有过去那么高，再让规则在新路径上从头跑。每种设定 300 条路径，晚一天成交，新旧权重跑在同样的路径上：")
    A("")
    A("| 平均每段 | 假设比特币今后年化 | 新权重年化 中位数（最差5% ~ 最好5%）| 新权重 3 年亏钱概率 | 新权重最大回撤 中位数 / 最差5% | 回撤超 45% 的路径 | 旧权重：年化中位数、亏钱概率、回撤中位/最差5% | 买入持有 3 年亏钱概率 |")
    A("|---|---|---|---|---|---|---|---|")
    for key, blk, name in (("90|None", "90 天", f"和 2018 年以来一样（约 {pc(fut['src_cagr'], 0)}）"), ("90|0.08", "90 天", "8%"), ("90|0.0", "90 天", "0%（不涨）"),
                           ("30|0.08", "30 天", "8%"), ("250|0.08", "250 天", "8%")):
        f = fut[key]
        nw, od = f["new"], f["old"]
        A(f"| {blk} | {name} | {pc(nw['median'], 0)}（{pc(nw['p5'], 0)} ~ {pc(nw['p95'], 0)}）| {pc(nw['loss'], 0)} | {pc(nw['dd_median'], 0)} / {pc(nw['dd_p95'], 0)} | {pc(nw['dd_over45'], 0)} | "
          f"{pc(od['median'], 0)}、{pc(od['loss'], 0)}、{pc(od['dd_median'], 0)} / {pc(od['dd_p95'], 0)} | {pc(f['bh']['loss'], 0)} |")
    A("")
    A(f"- 新旧对比：在同样的路径上，新权重回撤更小的占 {pc(min(fut[k]['new_better_dd'] for k in fut if k != 'src_cagr'), 0)}–{pc(max(fut[k]['new_better_dd'] for k in fut if k != 'src_cagr'), 0)}，"
      f"收益更高的占 {pc(min(fut[k]['new_better_cagr'] for k in fut if k != 'src_cagr'), 0)}–{pc(max(fut[k]['new_better_cagr'] for k in fut if k != 'src_cagr'), 0)}。和第 3 节的结论一致：降回撤，不增收。")
    A("- 分段越短，趋势被打得越碎，对趋势规则越不利（每段 30 天时接近「没有趋势可跟」的情形）。")
    A(f"- **采用的预期**：年化约 10–20%；任意 3 年亏钱的可能约 1/4 到 1/3；账面回撤按 -45% 到 -50% 做准备；可能连续两三年不创新高（新权重历史最长 {R['path']['longest_underwater_days']} 天）。")
    A(f"- 和另一份独立红队研究对照（旧权重）：它得到「比特币年化 8% 时规则中位数 14%、亏钱概率 29%、回撤中位数/最差5% 为 -34%/-49%」（每段约 250 天）；"
      f"本报告独立实现，旧权重同样设定下是 {pc(fut['250|0.08']['old']['median'], 0)}、{pc(fut['250|0.08']['old']['loss'], 0)}、{pc(fut['250|0.08']['old']['dd_median'], 0)}/{pc(fut['250|0.08']['old']['dd_p95'], 0)}。两边大体一致（差别在抽样误差和实现细节的范围内），都支持上面的预期。")
    A("- 局限：模拟的原料仍是 2018 年以来的比特币，只含约 3 轮牛熊；如果今后市场长期横盘震荡，规则会比模拟更差。")
    A("")
    A("## 8. 和「每月定投并持有」比")
    A("")
    A(f"110 USDT 起步，每 30 天存 50 USDT，持有 3 年；从 2016-01 到 2023-11 每月取一个起点，共 {pl['n']} 个：")
    A("")
    A("| 方案 | 期末资产 ÷ 累计投入：中位数 | 最差 | 最好 | 过程中最深的账面浮亏：中位数 | 最差 |")
    A("|---|---|---|---|---|---|")
    for key, name in (("dca", "存入就买、一直持有"), ("new", "新权重"), ("old", "旧权重"), ("half", "一半BTC一半USDT，每月调平")):
        v = pl[key]
        A(f"| {name} | {v['final_median']:.2f} 倍 | {v['final_min']:.2f} | {v['final_max']:.2f} | {pc(v['worst_median'], 0)} | {pc(v['worst_min'], 0)} |")
    A("")
    A(f"- 新权重最终比定投赚得多的起点只占 {pc(pl['rule_beats_dca'], 0)}（2018 年以后的起点里占 {pc(pl['rule_beats_dca_2018'], 0)}）。")
    A(f"- 规则的好处是过程：最深浮亏 {pc(pl['new']['worst_min'], 0)}，定投持有是 {pc(pl['dca']['worst_min'], 0)}；最差的 3 年期末是投入的 {pl['new']['final_min']:.2f} 倍。")
    A("- 所以实盘从第一天起同时记四本账（实盘新权重、旧权重、同样存款的定投持有、一半一半），周报里并排显示，用真实数据判断这套规则值不值。")
    A("")
    A("## 9. 账户熔断（回撤 45% 全部卖出、暂停 30 天、之后半仓）")
    A("")
    A("| 触发线 | 2015-10 起 | 触发日 | 2018-01 起 | 触发日 |")
    A("|---|---|---|---|---|")
    for th in ("None", "0.25", "0.3", "0.35", "0.4", "0.45"):
        a, b_ = R["breaker"][f"{th}|2015-10-01"], R["breaker"][f"{th}|2018-01-01"]

        def ev(x):
            t = [e[0] for e in x["events"] if e[1] == "tripped"]
            return "、".join(t) if t else "从未"
        A(f"| {'不设' if th == 'None' else pc(float(th), 0)}{'（现用）' if th == '0.45' else ''} | {cell(a)} | {ev(a)} | {cell(b_)} | {ev(b_)} |")
    over = [fut[k]["new"]["dd_over45"] for k in fut if k != "src_cagr"]
    A("")
    A("- 回测里熔断触发后假设用户一直不确认：半仓运行满 12 个月后自动恢复。")
    A(f"- -45% 在历史上从未触发。在第 7 节的模拟里，回撤超过 45% 的路径占 {pc(min(over), 0)}–{pc(max(over), 0)}。")
    t25a, t25b = R["breaker"]["0.25|2015-10-01"], R["breaker"]["0.25|2018-01-01"]
    if t25a["events"] or t25b["events"]:
        A(f"- 收紧到 25% 会在普通的熊市回撤里触发：2015-10 起的年化从 {pc(R['breaker']['None|2015-10-01']['cagr'])} 掉到 {pc(t25a['cagr'])}，2018 年起从 {pc(R['breaker']['None|2018-01-01']['cagr'])} 掉到 {pc(t25b['cagr'])}，最大回撤几乎没有改善。"
          "所以它只是「规则可能失效了，请人来看看」的警报，**不要收紧**。")
    A("- 回撤按「单位净值」算（存取款不计入盈亏），所以往账户里存钱不会掩盖亏损，取钱也不会误触发。")
    A("")
    A("## 10. 小账户能不能执行")
    A("")
    A("- 欧易 BTC-USDT 现货最小下单 0.00001 BTC（约 0.6 USDT；模拟盘是 0.00002），数量精度 0.00000001，价格精度 0.1（2026-11-21 从欧易公开接口读取；脚本每次运行都会重新读取）。110 USDT 的账户，调仓门槛 20% 对应每笔至少约 22 USDT，远高于最小下单量。")
    A(f"- 每年约 {n1['trades_per_year']:.0f} 笔，年换手约 {n1['turnover']:.1f} 倍，按 0.15% 的单笔成本算每年约 {pc(n1['turnover'] * 0.0015, 1)}。")
    A("- 脚本用「带价格上限的立即成交单」（IOC 限价单，价格上限 = 盘口价 ± 0.3%）：百元级订单在 BTC-USDT 上会立刻按盘口成交，上限只是防止极端瞬间滑点；没成交的部分自动撤销，当天不再重试。")
    A("")
    A("## 11. 局限与没有验证的事")
    A("")
    A("- 只有约 9 年、3 轮牛熊的数据；夏普比率的统计误差很大；所有参数（包括新权重的 0.3）都是看过这段历史后选的。")
    A("- 机构和 ETF 时代趋势效应会不会减弱，没法验证（2022 年以后确实比之前弱）。")
    A("- 没有建模：USDT 脱锚、欧易平台或账户风险、API 故障、极端行情下交易所暂停交易。")
    A("- 回撤按日收盘价算，盘中会更深一些（多 1–2 个点）。")
    A("- 实盘下单流程没有用真实 API key 跑过（没有 key）：公开行情接口和模拟盘公开接口实测通过，下单、查余额、查权限用模拟对象测试。上实盘前必须先在欧易模拟盘跑通。")
    A("")
    A("## 12. 如何复现")
    A("")
    A("```")
    A("pip install -r requirements.txt")
    A("python backtest/fetch_data.py        # 下载公开行情到 data/（不需要任何 key）")
    A("python backtest/run_all.py           # 约 6 分钟，重新生成本报告和图")
    A("python backtest/reproduce_review.py  # 用评审自己的代码重跑它的数字")
    A("```")
    A("")
    A("回测引擎 `backtest/engine.py` 直接调用实盘脚本的 `btc/strategy.py`（信号、调仓判断、熔断是同一份代码）。全部明细数字在 `data/bt_results.json`。")
    A("")
    (DOCS / "backtest-report.md").write_text("\n".join(L), encoding="utf-8")


def main():
    t0 = time.time()
    if "--report-only" in sys.argv or "--charts-only" in sys.argv:   # rebuild from the saved results
        R.update(json.loads((DATA / "bt_results.json").read_text(encoding="utf-8")))
        if "--charts-only" in sys.argv:
            saved = dict(R)
            y, _, _ = load_all()
            R.update(saved)
            charts(y, run(y, "2018-01-01", 1), run(y, "2018-01-01", 1, OLD), simulate_static(y.days, y.close, "2018-01-01", END))
        write_report()
        print("rebuilt")
        return
    y, b, o = load_all()
    hourly = load_hourly()
    rule_run = old_run = bh_run = None
    for name in ("review", "baseline", "compare", "sensitivity", "breaker", "eth", "plans", "future"):
        t = time.time()
        if name == "review":
            sec_review()
        elif name == "baseline":
            rule_run, old_run, bh_run = sec_baseline(y, b, o, hourly)
        elif name == "compare":
            sec_compare(y)
        elif name == "sensitivity":
            sec_sensitivity(y, hourly)
        elif name == "breaker":
            sec_breaker(y)
        elif name == "eth":
            sec_eth()
        elif name == "plans":
            sec_plans(y)
        else:
            sec_future(y, rule_run)
        print(f"{name:12s} {time.time() - t:6.1f}s", flush=True)
    charts(y, rule_run, old_run, bh_run)
    (DATA / "bt_results.json").write_text(json.dumps(R, indent=1, default=str, ensure_ascii=False), encoding="utf-8")
    write_report()
    print(f"done in {time.time() - t0:.0f}s -> {DOCS / 'backtest-report.md'}")


if __name__ == "__main__":
    main()
