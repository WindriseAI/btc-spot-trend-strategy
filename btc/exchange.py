"""OKX spot client: the only module that talks to the private API.

Design for safety rather than flexibility:
  * One instrument only: BTC-USDT spot. One trade mode only: cash (no margin).
  * One order type only: IOC limit (a marketable limit with a price cap).
  * Exactly one function sends a POST (`_post_order`), and it re-checks the
    request before sending. There is no code path to withdrawals, transfers,
    margin, swaps, futures or options.
  * ccxt is used for transport, signing, demo-mode header and error classes,
    but requests go to OKX's raw v5 endpoints so nothing is added implicitly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .config import Keys

INST = "BTC-USDT"
TD_MODE = "cash"
ORD_TYPE = "ioc"
_TAG_RE = re.compile(r"^[A-Za-z0-9]{1,16}$")


class ExchangeError(Exception):
    pass


class SafetyError(Exception):
    """A safety rule would be violated. Never caught-and-ignored."""


def validate_tag(tag: object) -> str:
    """Validate OKX order tag (AI Builder Code): 1-16 ASCII alphanumerics."""
    if not isinstance(tag, str) or not _TAG_RE.fullmatch(tag):
        raise SafetyError("OKX AI Builder Code (tag) 必须是 1~16 位英文字母或数字")
    return tag


@dataclass(frozen=True)
class Instrument:
    min_size: float
    lot: float
    tick: float
    state: str


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    last: float
    ts_ms: int


@dataclass(frozen=True)
class KeyInfo:
    perms: tuple
    ip_bound: bool
    account_level: str

    @property
    def can_withdraw(self) -> bool:
        return "withdraw" in self.perms

    @property
    def can_trade(self) -> bool:
        return "trade" in self.perms


@dataclass(frozen=True)
class Balances:
    btc: float            # cash balance
    usdt: float
    btc_avail: float      # not frozen by open orders
    usdt_avail: float
    liabilities: float    # any borrowed amount (must be 0)


@dataclass(frozen=True)
class OrderResult:
    found: bool
    state: str = ""       # filled | partially_filled | canceled | live | mmp_canceled
    filled: float = 0.0   # BTC
    avg_price: float = 0.0
    fee: float = 0.0      # positive number
    fee_ccy: str = ""
    ord_id: str = ""


def _num(x, default: float = 0.0) -> float:
    try:
        return float(x) if x not in (None, "") else default
    except (TypeError, ValueError):
        return default


_HINTS = {
    "50119": "API key 不存在（可能填错了，或把实盘 key 填到了模拟盘位置／反过来）",
    "50111": "API key 无效（请检查是否填错）",
    "50113": "签名无效（Secret 可能填错了）",
    "50105": "口令 Passphrase 不对",
    "50110": "当前机器的 IP 不在这个 API key 绑定的 IP 名单里",
    "50101": "API key 和环境不匹配（模拟盘 key 只能用于模拟盘，实盘 key 只能用于实盘）",
    "50102": "本机时间不准，请同步系统时钟",
    "50120": "这个 API key 权限不足",
}


def _hint(msg: str) -> str:
    for code, text in _HINTS.items():
        if f'"{code}"' in msg:
            return text + "。原始信息："
    return ""


def fmt_num(x: float, step: float) -> str:
    """Decimal string on the step grid, no exponent (OKX wants plain strings)."""
    import math
    decimals = max(0, -int(math.floor(math.log10(step) + 1e-12)))
    return f"{x:.{decimals}f}"


class OkxSpot:
    def __init__(
        self,
        keys: Optional[Keys],
        demo: bool,
        hostname: str = "www.okx.com",
        proxy: Optional[str] = None,
        client=None,
        builder_code: Optional[str] = None,
    ):
        self.demo = demo
        self.builder_code: Optional[str] = (
            validate_tag(builder_code) if builder_code is not None and builder_code != "" else None
        )
        if client is not None:          # tests inject a fake
            self.c = client
            return
        try:
            import ccxt
        except ImportError as e:  # pragma: no cover
            raise ExchangeError("缺少 ccxt 库，请先运行: pip install -r requirements.txt") from e
        cfg = {"enableRateLimit": True, "timeout": 20000, "hostname": hostname, "options": {"defaultType": "spot"}}
        if keys is not None:
            cfg.update(apiKey=keys.api_key, secret=keys.secret, password=keys.passphrase)
        self.c = ccxt.okx(cfg)
        if proxy:
            self.c.https_proxy = proxy
        self.c.set_sandbox_mode(bool(demo))   # adds the x-simulated-trading: 1 header
        if bool(self.c.headers.get("x-simulated-trading") == "1") != bool(demo):
            raise SafetyError("demo/live header mismatch")

    # ---- wrappers -------------------------------------------------------
    def _call(self, fn_name: str, params: Optional[dict] = None):
        try:
            return getattr(self.c, fn_name)(params or {})
        except Exception as e:  # noqa: BLE001
            # ccxt error text contains the URL and OKX's message, never the secret
            raise ExchangeError(f"{_hint(str(e))}{fn_name}: {type(e).__name__}: {str(e)[:300]}") from e

    # ---- public ---------------------------------------------------------
    def instrument(self) -> Instrument:
        d = self._call("public_get_public_instruments", {"instType": "SPOT", "instId": INST})["data"][0]
        return Instrument(float(d["minSz"]), float(d["lotSz"]), float(d["tickSz"]), d.get("state", ""))

    def quote(self) -> Quote:
        d = self._call("public_get_market_ticker", {"instId": INST})["data"][0]
        return Quote(float(d["bidPx"]), float(d["askPx"]), float(d["last"]), int(d["ts"]))

    # ---- private, read only --------------------------------------------
    def key_info(self) -> KeyInfo:
        d = self._call("private_get_account_config")["data"][0]
        perms = tuple(x.strip() for x in str(d.get("perm", "")).split(",") if x.strip())
        return KeyInfo(perms, bool(str(d.get("ip", "")).strip()), str(d.get("acctLv", "")))

    def balances(self) -> Balances:
        d = self._call("private_get_account_balance", {"ccy": "BTC,USDT"})["data"][0]
        by = {x["ccy"]: x for x in d.get("details", [])}

        def cash(c):
            x = by.get(c, {})
            return _num(x.get("cashBal"), _num(x.get("eq")))

        def avail(c):
            x = by.get(c, {})
            return min(cash(c), _num(x.get("availBal"), cash(c)))
        liab = sum(abs(_num(x.get("liab"))) for x in by.values())
        return Balances(max(0.0, cash("BTC")), max(0.0, cash("USDT")), max(0.0, avail("BTC")), max(0.0, avail("USDT")), liab)

    def open_derivative_positions(self) -> list:
        """Margin/swap/futures/option positions on the account (we never touch them)."""
        rows = self._call("private_get_account_positions")["data"]
        return [f"{r.get('instId')}({r.get('pos')})" for r in rows if abs(_num(r.get("pos"))) > 0]

    def taker_fee(self, default: float) -> float:
        try:
            d = self._call("private_get_account_trade_fee", {"instType": "SPOT", "instId": INST})["data"][0]
            v = abs(_num(d.get("taker"), default))
            return v if 0 <= v < 0.01 else default
        except Exception:  # noqa: BLE001
            return default

    def get_order(self, cl_ord_id: str) -> OrderResult:
        try:
            rows = self.c.private_get_trade_order({"instId": INST, "clOrdId": cl_ord_id})["data"]
        except Exception as e:  # noqa: BLE001
            if "51603" in str(e) or type(e).__name__ == "OrderNotFound":
                return OrderResult(False)
            raise ExchangeError(f"get_order: {type(e).__name__}: {str(e)[:300]}") from e
        if not rows:
            return OrderResult(False)
        r = rows[0]
        return OrderResult(True, r.get("state", ""), _num(r.get("accFillSz")), _num(r.get("avgPx")),
                           abs(_num(r.get("fee"))), r.get("feeCcy", ""), r.get("ordId", ""))

    # ---- the only write -------------------------------------------------
    def place_ioc(self, side: str, size: float, limit_price: float, cl_ord_id: str, inst: Instrument) -> str:
        """Send one IOC limit order on BTC-USDT spot. Returns OKX's order id."""
        req = {
            "instId": INST,
            "tdMode": TD_MODE,
            "side": side,
            "ordType": ORD_TYPE,
            "sz": fmt_num(size, inst.lot),
            "px": fmt_num(limit_price, inst.tick),
            "clOrdId": cl_ord_id,
        }
        if self.builder_code is not None:
            req["tag"] = self.builder_code
        return self._post_order(req)

    def _post_order(self, req: dict) -> str:
        required = {"instId", "tdMode", "side", "ordType", "sz", "px", "clOrdId"}
        optional = {"tag"}
        keys = set(req)
        if not required.issubset(keys) or not keys.issubset(required | optional):
            raise SafetyError(f"unexpected order fields: {sorted(keys ^ required)}")
        if "tag" in req:
            validate_tag(req["tag"])
        if req["instId"] != INST or req["tdMode"] != TD_MODE or req["ordType"] != ORD_TYPE:
            raise SafetyError("only BTC-USDT spot / cash / IOC orders are allowed")
        if req["side"] not in ("buy", "sell"):
            raise SafetyError("bad side")
        if not (float(req["sz"]) > 0 and float(req["px"]) > 0):
            raise SafetyError("bad size/price")
        if not (req["clOrdId"].isalnum() and len(req["clOrdId"]) <= 32):
            raise SafetyError("bad clOrdId")
        try:
            resp = self.c.private_post_trade_order(req)
        except Exception as e:  # noqa: BLE001
            raise ExchangeError(f"place_order: {type(e).__name__}: {str(e)[:300]}") from e
        row = (resp.get("data") or [{}])[0]
        if resp.get("code") != "0" or row.get("sCode", "0") != "0":
            raise ExchangeError(f"下单被拒绝: code={resp.get('code')} sCode={row.get('sCode')} {row.get('sMsg', resp.get('msg', ''))}")
        return row.get("ordId", "")
