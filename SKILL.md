---
name: btc-spot-trend-okx
description: Daily BTC-USDT spot trend-following strategy for OKX (long-only, no leverage, IOC limit orders, max 1 order/day). Supports read-only signal reports, OKX demo (paper) trading, live trading with explicit confirmation, and optional OKX AI Builder Code order attribution via the `tag` field.
metadata:
  author: WindriseAI
  version: "1.0.0"
---

# BTC Spot Trend Strategy (OKX)

> **⚠️ RISK DISCLAIMER — READ FIRST**
>
> **This is NOT investment advice. You bear all profits and losses; the author assumes no liability for any loss.**
> Only use funds you can afford to lose.
>
> Expected profile (from backtest & stress analysis — past performance does not guarantee future results):
> - Long-term annualized return expectation: **~10–20%** (lower than raw backtest 23–25%)
> - Potential maximum drawdown: **-45% to -50%**
> - Probability of losing money over any 3-year window: **~1/4 to 1/3**
> - Multi-year flat periods without new highs are normal; lags buy-and-hold in V-shaped recoveries and strong bull runs.

## Overview

This skill runs a deterministic daily **BTC-USDT spot-only** trend & volatility-scaled rebalancing strategy on OKX using the user's own OKX API keys:

- **Spot long-only, zero leverage**: Only `BTC-USDT`, `tdMode="cash"`, `ordType="ioc"` limit orders with a slippage cap.
- **Hard safety rails**: No withdrawal, transfer, margin, futures, swap, or option code paths exist. Refuses to run if the API key has `withdraw` permission, if derivative/margin positions or liabilities exist, or if dual-source market data checks fail.
- **Idempotent**: At most one order per UTC signal date (write-ahead state lock + deterministic `clOrdId`).
- **OKX AI Builder Code disclosure**: If `OKX_AI_BUILDER_CODE` is configured, orders attach the builder code to the OKX OpenAPI `tag` field (1–16 ASCII alphanumerics) so the author receives OKX's builder fee rebate. **User trading fees do not increase**, and the author never sees or stores the user's API keys.

## Prerequisites

- Python 3.10+
- Install dependencies in the repository root:
  ```bash
  pip install -r requirements.txt
  ```
- User's own OKX API key created on OKX:
  - Permissions: **Read + Trade ONLY** (**NEVER enable Withdraw**).
  - Recommended: dedicated **sub-account** with **IP whitelist** bound.
  - Store keys locally in `.env` (copied from `.env.example`) or environment variables. Never paste keys into chat or commit `.env`.

## Environment Variables

| Variable | Required For | Default | Description |
|---|---|---|---|
| `OKX_DEMO_API_KEY` | `paper` | — | User's OKX Demo Trading API key |
| `OKX_DEMO_API_SECRET` | `paper` | — | User's OKX Demo Trading API secret |
| `OKX_DEMO_API_PASSPHRASE` | `paper` | — | User's OKX Demo Trading passphrase |
| `OKX_API_KEY` | `live` | — | User's OKX Live API key (Read + Trade only, no Withdraw) |
| `OKX_API_SECRET` | `live` | — | User's OKX Live API secret |
| `OKX_API_PASSPHRASE` | `live` | — | User's OKX Live passphrase |
| `LIVE_CONFIRM` | `live` | empty | Must be `yes` to allow `python -m btc live` |
| `OKX_AI_BUILDER_CODE` | optional | empty | OKX AI Builder Code sent in order `tag` (1–16 ASCII letters/digits; invalid values raise `SafetyError`) |
| `STATE_DIR` | optional | `./state` | Persistent directory for NAV, drawdown breaker, and trade history (**do not delete**) |
| `SPOT_FEE` | optional | `0.001` | Fallback spot taker fee rate (0–0.01) |
| `SLIPPAGE_CAP` | optional | `0.003` | Max IOC limit price offset vs. best bid/ask (0 < cap <= 0.004) |
| `MAX_ORDER_USDT` | optional | unlimited | Optional per-order notional cap in USDT |
| `OKX_PROXY` | optional | empty | Optional HTTP/HTTPS proxy URL, e.g. `http://127.0.0.1:7890` |
| `OKX_HOSTNAME` | optional | `www.okx.com` | OKX API hostname |

## Commands

Run from the repository root (recommended daily after UTC 00:00 close, e.g. 00:22 UTC / 08:22 UTC+8):

```bash
# 1. Read-only daily signal report (no keys required, never places orders)
python -m btc report

# 2. Demo / paper trading on OKX Demo Trading (recommended for 2–4 weeks first)
python -m btc paper

# 3. Live trading on OKX Spot (requires LIVE_CONFIRM=yes and live keys)
python -m btc live

# 4. Weekly summary report (read-only)
python -m btc weekly

# 5. Manual confirmation to restore full sizing after a 45% drawdown circuit breaker pause
python -m btc confirm-resume live
```

Exit code `0` means normal (including hold / no trade needed). Exit code `1` (or output starting with `⚠️`) means human attention is required.

## Agent Operating Rules

When an AI agent (Claude Code, OpenClaw, etc.) operates this skill on behalf of a user:

1. **Always start safe**: Default to `python -m btc report` or `python -m btc paper`. **Never** switch to `python -m btc live`, set `LIVE_CONFIRM=yes`, or run `confirm-resume` without the user's explicit instruction.
2. **Never expose secrets**: Never print, log, or send `OKX_*_API_KEY`, `OKX_*_API_SECRET`, or `OKX_*_API_PASSPHRASE` in chat messages or logs.
3. **Respect safety blocks**: If the program refuses to trade (missing keys, withdraw permission, open derivatives, data mismatch, invalid `tag`, circuit breaker), report the exact output to the user—never bypass the script or place manual orders.
4. **Preserve state**: Keep `state/` (or `STATE_DIR`) persistent across runs so drawdown tracking, circuit breaker state, and same-day idempotency remain intact.
5. **See also**: Full Chinese user guide in `docs/给用户的说明.md` and strategy details in `README.md`.
