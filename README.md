# Autonomous Paper Trader

Multi-symbol, limit-order, event-driven paper trading bot. **Funny money only — no broker, no keys, no real orders.** Uses free market data via yfinance.

## What it does
- Scans a watchlist (default: SPY, USO, UCO, XLE, UNG, GLD — stocks + commodity ETFs)
- 30-min bars, EMA9/21 crossover + RSI filter for signals
- Places **limit orders** (entries at a target price, fill only if price touches) — maximizes entry price vs market orders
- Bracket risk on every position: stop-loss + take-profit
- Daily loss limit (default 4%) + kill switch
- **Conviction overrides:** you issue a call, it sizes and executes within rails
- Persists state between runs; writes a dashboard you can open in a browser

## Launch (free, ~5 min)
1. Create a free GitHub account (if you don't have one).
2. Create a new repository, e.g. `paper-trader`. Upload these files (`trader.py`, `requirements.txt`, `.github/workflows/trade.yml`, this README).
3. Push to GitHub. The workflow runs every 30 min during US market hours (Mon-Fri).
4. Enable **Settings → Pages → Deploy from branch** on `main` / root so `dashboard.html` is viewable at `https://<your-name>.github.io/paper-trader/dashboard.html`.
5. To issue a conviction trade, edit `commands.json` and commit it:
   ```json
   {"cmd":"conviction_long","symbol":"USO","level":"high"}
   ```
   Levels: `normal` (≤10% notional), `high` (≤25%), `extreme` (≤50%), `allin` (100% × LEVERAGE — Dan's override, mandatory stop still applies).

## Conviction commands
| cmd | symbol | level | effect |
|-----|--------|-------|--------|
| `conviction_long` | USO | high | open/replace a long, sized to high conviction |
| `conviction_short` | UCO | extreme | open/replace a short (shorts off by default) |
| `conviction_long` | USO | allin | deploy ~100% of equity (× LEVERAGE) on a high-conviction call |
| `close` | GLD | — | close that symbol's position |
| `flatten_all` | — | — | close everything, cancel pending |

## Config (env vars or GitHub repo variables)
`WATCHLIST`, `START_CASH`, `INTERVAL`, `FAST_EMA`, `SLOW_EMA`, `RSI_PERIOD`, `STOP_PCT`, `TARGET_PCT`, `RISK_PCT`, `DAILY_LOSS_LIMIT_PCT`, `MAX_POSITION_PCT`, `MAX_PORTFOLIO_EXPOSURE_PCT`, `MAX_ALLIN_NOTIONAL_PCT`, `LEVERAGE` (1=none, 2="double it" — liquidation risk), `KILL_SWITCH`, `ENABLE_SHORTS`.

## Going live (later, NOT now)
1. Paper-trade for several weeks. Review the equity curve and trade log.
2. Open a free Alpaca paper account for "real paper" (broker paper engine, live data).
3. Code review by Claude + Gemini (already required before any live money).
4. Fund a real Alpaca account, set `ALLOW_LIVE` only with explicit approval.

**DISCLAIMER:** Simulation only. Not financial advice. Past performance does not guarantee future results.
