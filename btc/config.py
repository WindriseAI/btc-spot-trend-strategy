"""Configuration from environment variables / a .env file. Nothing else.

Secrets are read here and handed straight to the exchange client; they are
never logged, printed or written to the state file.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Optional[Path] = None) -> None:
    """Minimal .env loader (KEY=VALUE per line). Real environment variables win."""
    path = path or Path(os.environ.get("BTC_ENV_FILE", ROOT / ".env"))
    if not Path(path).is_file():
        return
    for raw in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        os.environ.setdefault(key, val)


def _f(name: str, default: float) -> float:
    v = os.environ.get(name, "").strip()
    return float(v) if v else default


@dataclass(frozen=True)
class Keys:
    api_key: str = field(repr=False)
    secret: str = field(repr=False)
    passphrase: str = field(repr=False)

    def __repr__(self) -> str:  # never leak secrets through repr/logging
        return "Keys(***)"


@dataclass(frozen=True)
class Config:
    state_dir: Path
    live_keys: Optional[Keys]
    demo_keys: Optional[Keys]
    live_confirm: bool
    slippage_cap: float          # worst price we accept vs. best bid/ask
    max_source_diff: float       # max disagreement between price sources (latest close)
    max_order_usdt: Optional[float]
    min_order_usdt: float
    hostname: str                # OKX API host, e.g. www.okx.com
    proxy: Optional[str]
    default_fee: float           # spot taker fee used when the exchange does not tell us (SPOT_FEE)
    builder_code: Optional[str] = None  # optional OKX AI Builder Code sent via order `tag`

    def keys_for(self, account: str) -> Optional[Keys]:
        return {"live": self.live_keys, "demo": self.demo_keys}.get(account)


def _keys(prefix: str) -> Optional[Keys]:
    k = os.environ.get(f"{prefix}_API_KEY", "").strip()
    s = os.environ.get(f"{prefix}_API_SECRET", "").strip()
    p = os.environ.get(f"{prefix}_API_PASSPHRASE", "").strip()
    if not (k or s or p):
        return None
    if not (k and s and p):
        raise ConfigError(f"{prefix}_API_KEY / {prefix}_API_SECRET / {prefix}_API_PASSPHRASE 必须三个都填")
    return Keys(k, s, p)


class ConfigError(Exception):
    pass


def _fee() -> float:
    """Spot taker fee as a fraction (0.001 = 0.1%). SPOT_FEE is the documented name."""
    v = _f("SPOT_FEE", _f("DEFAULT_FEE", 0.001))
    if not 0 <= v <= 0.01:
        raise ConfigError("SPOT_FEE 应是小数形式的费率，例如 0.001 表示 0.1%（允许范围 0 到 0.01）")
    return v


def load_config(use_dotenv: bool = True) -> Config:
    if use_dotenv:
        load_dotenv()
    cap = _f("SLIPPAGE_CAP", 0.003)
    if not 0 < cap <= 0.004:
        # OKX rejects limit prices more than 0.5% through the book (floatPxLmtPct)
        raise ConfigError("SLIPPAGE_CAP 必须在 0 到 0.004 (0.4%) 之间")
    max_order = _f("MAX_ORDER_USDT", 0.0)
    return Config(
        state_dir=Path(os.environ.get("STATE_DIR", "").strip() or ROOT / "state"),
        live_keys=_keys("OKX"),
        demo_keys=_keys("OKX_DEMO"),
        live_confirm=os.environ.get("LIVE_CONFIRM", "").strip().lower() == "yes",
        slippage_cap=cap,
        max_source_diff=_f("MAX_SOURCE_DIFF", 0.015),
        max_order_usdt=max_order if max_order > 0 else None,
        min_order_usdt=_f("MIN_ORDER_USDT", 1.0),
        hostname=os.environ.get("OKX_HOSTNAME", "").strip() or "www.okx.com",
        proxy=os.environ.get("OKX_PROXY", "").strip() or None,
        default_fee=_fee(),
        builder_code=os.environ.get("OKX_AI_BUILDER_CODE", "").strip() or None,
    )
