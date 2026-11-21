"""Shared test fixtures: a fake OKX (mocking ccxt's raw endpoints) and fake market data.

No test touches the network or needs API keys.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from btc.config import Config, Keys  # noqa: E402
from btc.data import MarketData  # noqa: E402
from btc.exchange import OkxSpot  # noqa: E402
from btc.strategy import Signal  # noqa: E402

NOW = datetime(2026, 11, 21, 0, 22, tzinfo=timezone.utc)
SECRET = "s3cr3t-DO-NOT-PRINT"
PASSPHRASE = "pa55phrase-DO-NOT-PRINT"
APIKEY = "apikey-DO-NOT-PRINT"


class OrderNotFound(Exception):
    pass


class FakeOkxClient:
    """Stands in for ccxt.okx: implements only the raw endpoints we call."""

    def __init__(self, btc=0.0, usdt=120.0, bid=59999.9, ask=60000.0, perm="read_only,trade", ip="1.2.3.4",
                 positions=None, min_sz="0.00001", now=NOW, fee=0.001):
        self.btc, self.usdt = btc, usdt
        self.bid, self.ask = bid, ask
        self.perm, self.ip = perm, ip
        self.positions = positions or []
        self.min_sz = min_sz
        self.now = now
        self.fee = fee
        self.orders = {}          # clOrdId -> order row
        self.posts = []           # every order request sent
        self.calls = []
        self.fill = True          # False: IOC order is cancelled unfilled
        self.fail_post = None     # exception to raise from the POST
        self.fail_get_order = 0   # raise on the next N order queries
        self.liab = "0"
        self.headers = {}

    # ---- public
    def public_get_public_instruments(self, params):
        self.calls.append("instruments")
        assert params == {"instType": "SPOT", "instId": "BTC-USDT"}
        return {"code": "0", "data": [{"minSz": self.min_sz, "lotSz": "0.00000001", "tickSz": "0.1", "state": "live"}]}

    def public_get_market_ticker(self, params):
        self.calls.append("ticker")
        return {"code": "0", "data": [{"bidPx": str(self.bid), "askPx": str(self.ask), "last": str(self.ask),
                                       "ts": str(int(self.now.timestamp() * 1000))}]}

    # ---- private, read
    def private_get_account_config(self, params):
        self.calls.append("config")
        return {"code": "0", "data": [{"perm": self.perm, "ip": self.ip, "acctLv": "1"}]}

    def private_get_account_balance(self, params):
        self.calls.append("balance")
        assert params == {"ccy": "BTC,USDT"}
        det = [{"ccy": "BTC", "cashBal": repr(self.btc), "availBal": repr(self.btc), "liab": "0"},
               {"ccy": "USDT", "cashBal": repr(self.usdt), "availBal": repr(self.usdt), "liab": self.liab}]
        return {"code": "0", "data": [{"details": det}]}

    def private_get_account_positions(self, params):
        self.calls.append("positions")
        return {"code": "0", "data": self.positions}

    def private_get_account_trade_fee(self, params):
        return {"code": "0", "data": [{"taker": f"-{self.fee}", "maker": "-0.0008"}]}

    def private_get_trade_order(self, params):
        self.calls.append("get_order")
        if self.fail_get_order > 0:
            self.fail_get_order -= 1
            raise RuntimeError("network down")
        o = self.orders.get(params["clOrdId"])
        if o is None:
            raise OrderNotFound('okx {"code":"51603","msg":"Order does not exist"}')
        return {"code": "0", "data": [o]}

    # ---- private, write (the only one)
    def private_post_trade_order(self, req):
        self.calls.append("post")
        self.posts.append(dict(req))
        if self.fail_post is not None:
            raise self.fail_post
        if req["clOrdId"] in self.orders:
            return {"code": "1", "data": [{"sCode": "51016", "sMsg": "Client order ID already exists"}]}
        sz, px = float(req["sz"]), float(req["px"])
        row = {"clOrdId": req["clOrdId"], "ordId": str(1000 + len(self.orders)), "state": "canceled",
               "accFillSz": "0", "avgPx": "", "fee": "0", "feeCcy": ""}
        if self.fill:
            if req["side"] == "buy":
                assert px >= self.ask, "buy limit below the ask would not fill"
                cost = sz * self.ask
                assert cost <= self.usdt + 1e-9, "fake exchange: insufficient USDT (script tried to overspend)"
                fee = sz * self.fee
                self.usdt -= cost
                self.btc += sz - fee
                row.update(state="filled", accFillSz=req["sz"], avgPx=str(self.ask), fee=f"-{fee:.10f}", feeCcy="BTC")
            else:
                assert px <= self.bid
                assert sz <= self.btc + 1e-12, "fake exchange: insufficient BTC (script tried to oversell)"
                gross = sz * self.bid
                fee = gross * self.fee
                self.btc -= sz
                self.usdt += gross - fee
                row.update(state="filled", accFillSz=req["sz"], avgPx=str(self.bid), fee=f"-{fee:.10f}", feeCcy="USDT")
        self.orders[req["clOrdId"]] = row
        return {"code": "0", "data": [{"ordId": row["ordId"], "clOrdId": req["clOrdId"], "sCode": "0", "sMsg": ""}]}


def make_signal(T: float, close: float = 60000.0) -> Signal:
    """A Signal with a chosen target (E=m/7 and V chosen to multiply to T)."""
    if T <= 0:
        return Signal(close, (), (20, 40, 60, 90, 120, 180, 240), 0, 7, 0.0, 0.5, 0.8, 0.0)
    m = 7
    return Signal(close, (20, 40, 60, 90, 120, 180, 240), (), m, 7, 1.0, 0.4 / T, T, T)


def fake_md(T: float, now: datetime = NOW, ok: bool = True, problems=None) -> MarketData:
    md = MarketData(signal_date=now.date() - timedelta(days=1))
    md.signal = make_signal(T)
    md.closes = [60000.0] * 241
    md.primary, md.checker, md.diff_last = "OKX", "Binance", 0.0001
    md.checker_signal = md.signal
    md.problems = list(problems or ([] if ok else ["测试：数据有问题"]))
    md.ok_to_trade = not md.problems
    return md


@pytest.fixture
def cfg(tmp_path):
    def build(live_confirm=False, live=True, demo=True, **kw):
        base = dict(
            state_dir=tmp_path / "state",
            live_keys=Keys(APIKEY, SECRET, PASSPHRASE) if live else None,
            demo_keys=Keys(APIKEY, SECRET, PASSPHRASE) if demo else None,
            live_confirm=live_confirm, slippage_cap=0.003, max_source_diff=0.015,
            max_order_usdt=None, min_order_usdt=1.0, hostname="www.okx.com", proxy=None, default_fee=0.001,
            builder_code=None,
        )
        base.update(kw)
        return Config(**base)
    return build


class Harness:
    """Runs the engine against a fake exchange and a chosen target."""

    def __init__(self, cfg, client=None):
        self.cfg = cfg
        self.client = client or FakeOkxClient()
        self.T = 0.0
        self.md_kwargs = {}
        self.demo_flags = []

    def make_exchange(self, keys, demo, cfg):
        self.demo_flags.append(demo)
        return OkxSpot(keys, demo=demo, client=self.client, builder_code=cfg.builder_code)

    def run(self, mode="paper", T=None, now=NOW, **kw):
        from btc.engine import run_daily
        if T is not None:
            self.T = T
        self.client.now = now
        return run_daily(mode, self.cfg, now=now, make_exchange=self.make_exchange,
                         get_data=lambda n, p, d, h: fake_md(self.T, n, **self.md_kwargs), sleep=lambda s: None, **kw)


@pytest.fixture
def harness(cfg):
    def build(client=None, **cfg_kw):
        return Harness(cfg(**cfg_kw), client)
    return build


def day(n: int) -> datetime:
    return NOW + timedelta(days=n)


def d(s: str) -> date:
    return date.fromisoformat(s)
