"""
Autonomous paper trader — multi-symbol, limit-order, event-driven (v2, fixed).

PAPER / FUNNY MONEY ONLY. No broker, no keys, no real orders. Free data (yfinance).

Key safety rules (fixed after review):
  - Market-hours + fresh-bar guard: no trading actions when the market is closed
    or the latest bar is stale. On closed/stale runs, only render the dashboard.
  - No same-bar fills: a limit order placed on bar T can only fill on a bar
    strictly after T (tracked via created_bar_time + last_processed_bar_time).
  - Bracket exits (stop/target) evaluated only on genuinely new bars.
  - Commands are not cleared until successfully applied; failed commands persist.
  - conviction_short respects ENABLE_SHORTS; pending orders are cancelled before
    a manual conviction open and refused if a position already exists.
  - Position sizing by stop-risk (RISK_PCT), capped by per-position notional
    (MAX_POSITION_PCT) and portfolio exposure (MAX_PORTFOLIO_EXPOSURE_PCT).
  - Daily loss limit: day_start_equity set after prices load; when breached,
    allow exits only, cancel pending, block new entries (close/flatten allowed).

DISCLAIMER: Simulation only. Not financial advice. Before ANY real money:
paper results + Claude/Gemini review + Dan's explicit approval.
"""
import os
import json
import copy
import logging
import time as _time
from datetime import datetime, timezone, date, time as dtime

import pandas as pd
import yfinance as yf

# ----------------------------- Config -----------------------------
_DEFAULT_WATCH = "SPY,USO,UCO,XLE,UNG,GLD"
WATCHLIST = [s.strip().upper() for s in (os.environ.get("WATCHLIST") or _DEFAULT_WATCH).split(",") if s.strip()]
START_CASH = float(os.environ.get("START_CASH", "10000"))
INTERVAL = os.environ.get("INTERVAL", "30m")
LOOKBACK = os.environ.get("LOOKBACK", "60d")

FAST_EMA = int(os.environ.get("FAST_EMA", "9"))
SLOW_EMA = int(os.environ.get("SLOW_EMA", "21"))
RSI_PERIOD = int(os.environ.get("RSI_PERIOD", "14"))
RSI_LONG_MAX = float(os.environ.get("RSI_LONG_MAX", "70"))
RSI_SHORT_MIN = float(os.environ.get("RSI_SHORT_MIN", "30"))

RISK_PCT = float(os.environ.get("RISK_PCT", "1.0"))          # % equity risked at stop
STOP_PCT = float(os.environ.get("STOP_PCT", "2.0"))
TARGET_PCT = float(os.environ.get("TARGET_PCT", "4.0"))
DAILY_LOSS_LIMIT_PCT = float(os.environ.get("DAILY_LOSS_LIMIT_PCT", "4.0"))
MAX_POSITION_PCT = float(os.environ.get("MAX_POSITION_PCT", "25"))        # max notional per position
MAX_PORTFOLIO_EXPOSURE_PCT = float(os.environ.get("MAX_PORTFOLIO_EXPOSURE_PCT", "100"))  # total notional cap
LEVERAGE = float(os.environ.get("LEVERAGE", "1"))  # 1=none. 2="double it" (liquidation risk — paper only until approved)
MAX_ALLIN_NOTIONAL_PCT = float(os.environ.get("MAX_ALLIN_NOTIONAL_PCT", "100"))  # all-in cap (x LEVERAGE)
LIMIT_EXPIRE_BARS = int(os.environ.get("LIMIT_EXPIRE_BARS", "8"))
STALE_BAR_MIN = float(os.environ.get("STALE_BAR_MIN", "45"))  # bar older than this => market closed
ENABLE_SHORTS = os.environ.get("ENABLE_SHORTS", "false").lower() in ("1", "true", "yes")
KILL_SWITCH = os.environ.get("KILL_SWITCH", "false").lower() in ("1", "true", "yes")

CONVICTION_NOTIONAL = {"normal": 0.10, "high": 0.25, "extreme": 0.50, "allin": 1.0}

STATE_FILE = os.environ.get("STATE_FILE", "paper_state.json")
COMMANDS_FILE = os.environ.get("COMMANDS_FILE", "commands.json")
DASHBOARD_FILE = os.environ.get("DASHBOARD_FILE", "dashboard.html")
LOG_FILE = os.environ.get("LOG_FILE", "trader.log")
MIN_BARS = max(SLOW_EMA, RSI_PERIOD) + 5

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()])
log = logging.getLogger("trader")


# ----------------------------- State -----------------------------
DEFAULT_STATE = {
    "cash": START_CASH, "positions": {}, "pending": [], "trades": [],
    "equity_history": [], "day_start_equity": START_CASH, "last_day": None,
    "last_processed_bar": {},  # symbol -> ISO ts of last bar processed
}

def load_state():
    state = copy.deepcopy(DEFAULT_STATE)
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                loaded = json.load(f)
            loaded.pop("position", None)  # legacy single-position key
            state.update(loaded)
            for k in ("positions", "pending", "trades", "equity_history", "last_processed_bar"):
                state.setdefault(k, {} if k in ("positions", "last_processed_bar") else [])
        except Exception:
            pass
    return state

def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)


def load_commands():
    if not os.path.exists(COMMANDS_FILE):
        return []
    try:
        with open(COMMANDS_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, list) else [data]
    except Exception as e:
        log.warning(f"commands.json unreadable: {e}")
        return []

def save_commands(cmds):
    with open(COMMANDS_FILE, "w") as f:
        json.dump(cmds, f, indent=2)


# ----------------------------- Market data -----------------------------
def get_bars(symbol):
    df = yf.download(symbol, period=LOOKBACK, interval=INTERVAL, progress=False, auto_adjust=False)
    if df is None or df.empty:
        return None
    df.columns = [c[0] if isinstance(c, tuple) else str(c).replace(" ", "_") for c in df.columns]
    df = df.dropna(subset=["Close"]).sort_index()
    if len(df) < MIN_BARS:
        return None
    return df

def bar_status(df):
    """Return (last_ts, is_open). is_open = weekday, in US market hours, bar fresh."""
    last_ts = pd.Timestamp(df.index[-1])
    if last_ts.tzinfo is None:
        last_ts = last_ts.tz_localize("UTC")
    now = pd.Timestamp.now("UTC")
    age_min = (now - last_ts).total_seconds() / 60.0
    et = last_ts.tz_convert("US/Eastern")
    in_session = et.weekday() < 5 and dtime(9, 30) <= et.time() <= dtime(16, 15)
    is_open = in_session and age_min <= STALE_BAR_MIN
    return last_ts, is_open, age_min

def ema(s, p):
    return s.ewm(span=p, adjust=False).mean()

def rsi(s, p=14):
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / p, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / p, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))

def signal(df):
    c = df["Close"]
    f1, f2 = ema(c, FAST_EMA).iloc[-1], ema(c, FAST_EMA).iloc[-2]
    s1, s2 = ema(c, SLOW_EMA).iloc[-1], ema(c, SLOW_EMA).iloc[-2]
    r1 = rsi(c, RSI_PERIOD).iloc[-1]
    if any(v != v for v in (f1, f2, s1, s2, r1)):
        return "FLAT", r1
    bull = f2 <= s2 and f1 > s1
    bear = f2 >= s2 and f1 < s1
    if bull and r1 < RSI_LONG_MAX:
        return "LONG", r1
    if ENABLE_SHORTS and bear and r1 > RSI_SHORT_MIN:
        return "SHORT", r1
    return "FLAT", r1


# ----------------------------- Portfolio math -----------------------------
def unrealized_pos(pos, price):
    diff = price - pos["entry"] if pos["side"] == "LONG" else pos["entry"] - price
    return diff * pos["qty"]

def total_equity(state, prices):
    eq = state["cash"]
    for sym, pos in state["positions"].items():
        eq += unrealized_pos(pos, prices.get(sym, pos["entry"]))
    return eq

def exposure_used(state, prices):
    total = 0.0
    for sym, pos in state["positions"].items():
        total += abs(pos["qty"]) * prices.get(sym, pos["entry"])
    for o in state["pending"]:
        total += abs(o["qty"]) * o["limit_price"]
    return total


# ----------------------------- Sizing -----------------------------
def size_position(state, side, price, level, prices):
    """Size by conviction level, capped by stop-risk + portfolio exposure.
    allin bypasses risk sizing (Dan's override); the mandatory stop still bounds loss."""
    equity = total_equity(state, prices)
    if equity <= 0:
        return 0.0
    # ALL-IN: deploy up to MAX_ALLIN_NOTIONAL_PCT * LEVERAGE of equity (stop still bounds downside)
    if level == "allin":
        max_notional = equity * LEVERAGE * (MAX_ALLIN_NOTIONAL_PCT / 100.0)
        avail = max_notional - exposure_used(state, prices)
        return max(0.0, avail / price)
    stop_dist = price * (STOP_PCT / 100.0)
    if stop_dist <= 0:
        return 0.0
    notional_cap = equity * CONVICTION_NOTIONAL.get(level, CONVICTION_NOTIONAL["normal"])
    qty = notional_cap / price
    qty_by_risk = (equity * RISK_PCT / 100.0) / stop_dist
    qty = min(qty, qty_by_risk)  # never risk more than RISK_PCT at the stop
    if level == "normal":  # auto signals also respect per-position cap
        qty = min(qty, (equity * MAX_POSITION_PCT / 100.0) / price)
    remaining_notional = (equity * MAX_PORTFOLIO_EXPOSURE_PCT / 100.0) - exposure_used(state, prices)
    if remaining_notional <= 0:
        return 0.0
    return max(0.0, min(qty, remaining_notional / price))


# ----------------------------- Orders -----------------------------
def close_position(state, symbol, exit_price, reason, prices):
    pos = state["positions"].get(symbol)
    if not pos:
        return
    diff = exit_price - pos["entry"] if pos["side"] == "LONG" else pos["entry"] - exit_price
    pnl = diff * pos["qty"]
    state["cash"] += pnl
    state["trades"].append({
        "symbol": symbol, "side": pos["side"], "qty": pos["qty"], "entry": pos["entry"],
        "exit": exit_price, "pnl": round(pnl, 2), "reason": reason,
        "entry_time": pos["entry_time"], "exit_time": datetime.now(timezone.utc).isoformat(),
    })
    log.info(f"CLOSE {pos['side']} {symbol} {pos['qty']:.4f} @ {exit_price:.2f} ({reason}) P/L=${pnl:.2f}")
    del state["positions"][symbol]

def cancel_pending(state, symbol):
    before = len(state["pending"])
    state["pending"] = [o for o in state["pending"] if o["symbol"] != symbol]
    if len(state["pending"]) < before:
        log.info(f"CANCEL pending for {symbol}")

def check_bracket(state, symbol, bar, prices, bar_ts):
    """Exit if stop/target hit in this bar. Stop first (conservative)."""
    pos = state["positions"].get(symbol)
    if not pos:
        return
    hi, lo = float(bar["High"]), float(bar["Low"])
    if pos["side"] == "LONG":
        if lo <= pos["stop"]:
            close_position(state, symbol, pos["stop"], "stop-hit", prices)
        elif hi >= pos["target"]:
            close_position(state, symbol, pos["target"], "target-hit", prices)
    else:
        if hi >= pos["stop"]:
            close_position(state, symbol, pos["stop"], "stop-hit", prices)
        elif lo <= pos["target"]:
            close_position(state, symbol, pos["target"], "target-hit", prices)

def check_pending(state, symbol, bar, prices, bar_ts):
    """Fill a pending limit only if this bar is strictly after created_bar_time."""
    remaining = []
    for o in state["pending"]:
        if o["symbol"] != symbol:
            remaining.append(o)
            continue
        created = pd.Timestamp(o["created_bar_time"])
        if created.tzinfo is None:
            created = created.tz_localize("UTC")
        if bar_ts <= created:
            remaining.append(o)  # same bar or earlier — do not fill (no look-ahead)
            continue
        # refuse fill if a position already exists for this symbol
        if symbol in state["positions"]:
            log.info(f"CANCEL pending {o['side']} {symbol} (position exists)")
            continue
        hi, lo = float(bar["High"]), float(bar["Low"])
        touched = (lo <= o["limit_price"]) if o["side"] == "LONG" else (hi >= o["limit_price"])
        if touched:
            fill_limit_order(state, o)
        else:
            o["bars_alive"] = o.get("bars_alive", 0) + 1
            if o["bars_alive"] >= LIMIT_EXPIRE_BARS:
                log.info(f"CANCEL pending {o['side']} {symbol} limit={o['limit_price']:.2f} (expired)")
            else:
                remaining.append(o)
    state["pending"] = remaining

def fill_limit_order(state, order):
    sym, side = order["symbol"], order["side"]
    state["positions"][sym] = {
        "side": side, "qty": order["qty"], "entry": order["limit_price"],
        "stop": order["stop"], "target": order["target"],
        "entry_time": datetime.now(timezone.utc).isoformat(), "level": order.get("level", "normal"),
    }
    log.info(f"FILL {side} {sym} {order['qty']:.4f} @ {order['limit_price']:.2f} (limit order)")

def place_limit_order(state, symbol, side, price, level, prices, bar_ts):
    if symbol in state["positions"]:
        return
    qty = size_position(state, side, price, level, prices)
    if qty <= 0:
        return
    limit_price = price * (0.998 if side == "LONG" else 1.002)
    if side == "LONG":
        stop, target = limit_price * (1 - STOP_PCT / 100), limit_price * (1 + TARGET_PCT / 100)
    else:
        stop, target = limit_price * (1 + STOP_PCT / 100), limit_price * (1 - TARGET_PCT / 100)
    state["pending"].append({
        "symbol": symbol, "side": side, "qty": round(qty, 6), "limit_price": limit_price,
        "stop": stop, "target": target, "level": level,
        "created": datetime.now(timezone.utc).isoformat(),
        "created_bar_time": bar_ts.isoformat(), "bars_alive": 0,
    })
    log.info(f"LIMIT {side} {symbol} {qty:.4f} @ {limit_price:.2f} stop={stop:.2f} target={target:.2f} level={level}")

def open_position_now(state, symbol, side, price, level, prices):
    cancel_pending(state, symbol)
    if symbol in state["positions"]:
        close_position(state, symbol, price, "conviction-replace", prices)
    qty = size_position(state, side, price, level, prices)
    if qty <= 0:
        log.warning(f"conviction {side} {symbol}: sizing returned 0 (exposure cap?)")
        return
    if side == "LONG":
        stop, target = price * (1 - STOP_PCT / 100), price * (1 + TARGET_PCT / 100)
    else:
        stop, target = price * (1 + STOP_PCT / 100), price * (1 - TARGET_PCT / 100)
    state["positions"][symbol] = {
        "side": side, "qty": round(qty, 6), "entry": price, "stop": stop, "target": target,
        "entry_time": datetime.now(timezone.utc).isoformat(), "level": level,
    }
    log.info(f"OPEN {side} {symbol} {qty:.4f} @ {price:.2f} stop={stop:.2f} target={target:.2f} level={level}")


def apply_commands(state, commands, prices):
    """Apply commands; return the list NOT applied (to persist for next run)."""
    unapplied = []
    for c in commands:
        cmd = c.get("cmd")
        sym = c.get("symbol", "").upper()
        level = c.get("level", "high") if c.get("level") in CONVICTION_NOTIONAL else "high"
        if cmd == "flatten_all":
            for s in list(state["positions"].keys()):
                close_position(state, s, prices.get(s, state["positions"][s]["entry"]), "manual-flatten", prices)
            state["pending"] = []
            log.info("CMD flatten_all")
        elif cmd == "close" and sym:
            cancel_pending(state, sym)
            if sym in state["positions"]:
                close_position(state, sym, prices.get(sym, state["positions"][sym]["entry"]), "manual-close", prices)
            log.info(f"CMD close {sym}")
        elif cmd in ("conviction_long", "conviction_short") and sym:
            side = "LONG" if cmd == "conviction_long" else "SHORT"
            if side == "SHORT" and not ENABLE_SHORTS:
                log.warning(f"CMD conviction_short {sym} ignored (ENABLE_SHORTS=false)")
                continue  # drop it (don't keep retrying)
            price = prices.get(sym)
            if not price:
                log.warning(f"CMD conviction {sym}: no price, will retry next run")
                unapplied.append(c)
            else:
                open_position_now(state, sym, side, price, level, prices)
                log.info(f"CMD conviction {side} {sym} level={level}")
        else:
            log.warning(f"CMD unknown: {c}")
    return unapplied


# ----------------------------- Dashboard -----------------------------
def render_dashboard(state, prices, market_open):
    equity = total_equity(state, prices)
    rows = []
    for sym in WATCHLIST:
        pos = state["positions"].get(sym)
        price = prices.get(sym, 0)
        if pos:
            u = unrealized_pos(pos, price)
            color = "green" if u >= 0 else "red"
            rows.append(f"<tr><td>{sym}</td><td>{price:.2f}</td><td>{pos['side']} {pos['qty']:.4f}</td>"
                        f"<td>{pos['entry']:.2f}</td><td>{pos['stop']:.2f}</td><td>{pos['target']:.2f}</td>"
                        f"<td style='color:{color}'>${u:.2f}</td></tr>")
        else:
            rows.append(f"<tr><td>{sym}</td><td>{price:.2f}</td><td colspan='5'>flat</td></tr>")
    pending = "".join(
        f"<li>{o['side']} {o['symbol']} {o['qty']:.4f} @ {o['limit_price']:.2f} (stop {o['stop']:.2f} / target {o['target']:.2f})</li>"
        for o in state["pending"]) or "<li>none</li>"
    pnl = sum(t["pnl"] for t in state["trades"])
    status = "MARKET OPEN — trading active" if market_open else "MARKET CLOSED — dashboard only, no trades"
    html = f"""<!doctype html><html><head><meta charset='utf-8'><title>Paper Trader</title>
<meta http-equiv='refresh' content='60'>
<style>body{{font-family:Arial,sans-serif;background:#171614;color:#CDCCCA;margin:20px;}}
h1{{color:#4F98A3;}}table{{border-collapse:collapse;width:100%;}}td,th{{border:1px solid #393836;padding:6px;text-align:left;}}
th{{background:#1C1B19;}}.kpi{{font-size:24px;font-weight:bold;}} .muted{{color:#797876;}} .warn{{color:#BB653B;}}</style></head>
<body><h1>Paper Trader — Funny Money</h1>
<p class='{"muted" if market_open else "warn"}'>{status}. Updated {datetime.now(timezone.utc).isoformat()}</p>
<p>Cash: <span class='kpi'>${state['cash']:.2f}</span> &nbsp; Equity: <span class='kpi'>${equity:.2f}</span>
&nbsp; Realized P/L: <span class='kpi' style='color:{"green" if pnl>=0 else "red"}'>${pnl:.2f}</span>
&nbsp; Trades: {len(state['trades'])}</p>
<h2>Watchlist &amp; Positions</h2><table><tr><th>Symbol</th><th>Price</th><th>Position</th><th>Entry</th><th>Stop</th><th>Target</th><th>Unrealized</th></tr>
{''.join(rows)}</table>
<h2>Pending Limit Orders</h2><ul>{pending}</ul>
<p class='muted'>Conviction trade via commands.json: {{\"cmd\":\"conviction_long\",\"symbol\":\"USO\",\"level\":\"high\"}}</p>
</body></html>"""
    with open(DASHBOARD_FILE, "w") as f:
        f.write(html)


# ----------------------------- Main loop -----------------------------
def run():
    if KILL_SWITCH:
        log.info("KILL_SWITCH on — dashboard only.")
        state = load_state()
        render_dashboard(state, {}, False)
        return

    state = load_state()

    # 1) fetch bars once per symbol (include held/pending symbols in case watchlist changed)
    bars = {}
    prices = {}
    market_open = True
    active_symbols = sorted(set(WATCHLIST) | set(state["positions"].keys()) | {o["symbol"] for o in state["pending"]})
    for sym in active_symbols:
        df = get_bars(sym)
        if df is None:
            log.warning(f"No bars for {sym}")
            continue
        bars[sym] = df
        prices[sym] = float(df["Close"].iloc[-1])
        last_ts, is_open, age = bar_status(df)
        if not is_open:
            market_open = False
            log.info(f"{sym} last_bar={last_ts} age={age:.0f}min -> market closed/stale")

    # day-start equity (after prices known)
    today = date.today().isoformat()
    if state.get("last_day") != today:
        state["last_day"] = today
        state["day_start_equity"] = total_equity(state, prices)

    # 2) manage exits + pending on NEW bars (only when market open)
    if market_open:
        for sym, df in bars.items():
            last_ts, _, _ = bar_status(df)
            last_processed = state["last_processed_bar"].get(sym)
            new_bar = (pd.Timestamp(last_processed).tz_localize("UTC") if last_processed and pd.Timestamp(last_processed).tzinfo is None else (pd.Timestamp(last_processed) if last_processed else None))
            has_new = (new_bar is None) or (last_ts > new_bar)
            if not has_new:
                continue
            bar = df.iloc[-1]
            check_bracket(state, sym, bar, prices, last_ts)
            check_pending(state, sym, bar, prices, last_ts)
            state["last_processed_bar"][sym] = last_ts.isoformat()

    # 3) commands (allowed in any market state — Dan may flatten/close off-hours;
    #    conviction opens only when market open + not halted)
    commands = load_commands()
    still_pending = []
    if commands:
        equity = total_equity(state, prices)
        dse = state.get("day_start_equity", equity)
        daily_pl = (equity - dse) / dse * 100 if dse else 0
        halt = daily_pl <= -DAILY_LOSS_LIMIT_PCT
        applied_ok = []
        for c in commands:
            cmd = c.get("cmd")
            if cmd in ("flatten_all", "close"):
                apply_commands(state, [c], prices); applied_ok.append(c)
            elif cmd in ("conviction_long", "conviction_short"):
                if not market_open:
                    still_pending.append(c)  # wait for market open
                elif halt:
                    log.warning(f"CMD conviction ignored (daily loss halt): {c}")
                else:
                    apply_commands(state, [c], prices); applied_ok.append(c)
            else:
                apply_commands(state, [c], prices); applied_ok.append(c)

    # 4) daily loss limit
    equity = total_equity(state, prices)
    dse = state.get("day_start_equity", equity)
    daily_pl = (equity - dse) / dse * 100 if dse else 0
    halt = daily_pl <= -DAILY_LOSS_LIMIT_PCT
    if halt:
        log.warning(f"Daily loss limit hit: {daily_pl:.2f}% — exits only, no new entries.")
        state["pending"] = []  # cancel pending entries

    # 5) technical signals -> new limit orders (only when open, not halted)
    if market_open and not halt:
        for sym, df in bars.items():
            sig, r1 = signal(df)
            has_pos = sym in state["positions"]
            has_pending = any(o["symbol"] == sym for o in state["pending"])
            if has_pos or has_pending:
                action = "hold"
            elif sig in ("LONG", "SHORT"):
                last_ts, _, _ = bar_status(df)
                place_limit_order(state, sym, sig, prices[sym], "normal", prices, last_ts)
                action = f"limit-{sig}"
            else:
                action = "flat"
            log.info(f"{sym} price={prices[sym]:.2f} rsi={r1:.1f} signal={sig} action={action}")

    equity = total_equity(state, prices)
    state["equity_history"].append({"t": datetime.now(timezone.utc).isoformat(), "equity": round(equity, 2)})
    state["equity_history"] = state["equity_history"][-1000:]
    save_state(state)
    save_commands(still_pending)  # persist only unapplied commands, after state is safely saved
    render_dashboard(state, prices, market_open)
    log.info(f"STATUS market={'open' if market_open else 'closed'} equity=${equity:.2f} cash=${state['cash']:.2f} "
             f"dailyPL={daily_pl:.2f}% positions={list(state['positions'])} pending={len(state['pending'])} trades={len(state['trades'])}")


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        log.exception(f"Run failed: {e}")
