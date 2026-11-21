"""Optional checks against the real public internet (no keys, no orders).

Skipped unless RUN_NETWORK_TESTS=1, so the normal test run stays offline.
  RUN_NETWORK_TESTS=1 python -m pytest tests/test_network_smoke.py -q
"""
import os
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_NETWORK_TESTS") != "1", reason="set RUN_NETWORK_TESTS=1 to run")


def test_okx_demo_public_endpoints_through_ccxt():
    from btc.exchange import OkxSpot
    ex = OkxSpot(None, demo=True)
    assert ex.c.headers.get("x-simulated-trading") == "1"
    inst = ex.instrument()
    q = ex.quote()
    assert inst.state == "live" and 0 < inst.lot <= inst.min_size < 0.01 and inst.tick > 0
    assert 0 < q.bid <= q.ask and abs(datetime.now(timezone.utc).timestamp() - q.ts_ms / 1000) < 300


def test_okx_live_public_endpoints_through_ccxt():
    from btc.exchange import OkxSpot
    ex = OkxSpot(None, demo=False)
    assert "x-simulated-trading" not in ex.c.headers
    assert ex.instrument().min_size > 0 and ex.quote().ask > 0


def test_private_call_without_keys_fails_cleanly():
    from btc.exchange import ExchangeError, OkxSpot
    with pytest.raises(ExchangeError):
        OkxSpot(None, demo=True).key_info()


def test_every_price_source_parses_and_real_data_passes_the_checks():
    from btc import data as D
    now = D.okx_server_time() or datetime.now(timezone.utc)
    ok = 0
    for name, fn, _ in D.SOURCES:
        try:
            s = fn("www.okx.com")
            assert len(s) >= 241 and all(v > 0 for v in s.values())
            ok += 1
        except Exception as e:  # a single venue may be unreachable from some networks
            print("source failed:", name, e)
    assert ok >= 2
    md = D.get_market_data(now)
    assert md.signal is not None and md.ok_to_trade, md.problems
    assert len(md.closes) == 241 and len(md.closes_long) >= 241
