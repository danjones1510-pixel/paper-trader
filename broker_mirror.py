#!/usr/bin/env python3
"""Mirror the simulator's positions into an Alpaca account.

The simulator (trader.py) stays the decision-maker. After every run, this
module reconciles the broker account toward the simulator's open positions:

    target qty (sim)  vs  actual qty (Alpaca)  ->  market order for the difference

Reconciling (instead of replaying individual events) is idempotent: a missed
run, a rejected order, or a restart just gets corrected on the next run.
Every submitted order is logged with the simulator's price so real fills can
be compared to simulated ones (mirror_state.json, "fills").

Safety
- Disabled unless ALPACA_KEY_ID and ALPACA_SECRET_KEY are set.
- Paper endpoint by default. The live endpoint is refused unless ALLOW_LIVE=true.
- MIRROR_MAX_ORDER_USD caps any single order's notional.
- Orders only while Alpaca's clock says the market is open.
- Skips a symbol while it already has an open order (no duplicate submits).
- Never raises into trader.py; failures are logged and retried next run.

Run `python3 broker_mirror.py --check` to verify keys without trading.
"""
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone

import requests

log = logging.getLogger("trader")

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"
KEY_ID = os.environ.get("ALPACA_KEY_ID", "").strip()
SECRET = os.environ.get("ALPACA_SECRET_KEY", "").strip()
BASE_URL = (os.environ.get("ALPACA_BASE_URL") or PAPER_URL).rstrip("/")
ALLOW_LIVE = os.environ.get("ALLOW_LIVE", "false").lower() in ("1", "true", "yes")
MAX_ORDER_USD = float(os.environ.get("MIRROR_MAX_ORDER_USD", "15000"))
MIRROR_STATE_FILE = os.environ.get("MIRROR_STATE_FILE", "mirror_state.json")
QTY_EPS = 1e-6


class MirrorError(Exception):
    pass


def enabled():
    return bool(KEY_ID and SECRET)


def is_live():
    return BASE_URL != PAPER_URL


def _guard():
    if not enabled():
        raise MirrorError("Alpaca keys not configured")
    if is_live() and not ALLOW_LIVE:
        raise MirrorError(f"Refusing non-paper endpoint {BASE_URL} without ALLOW_LIVE=true")


def _req(method, path, **kw):
    _guard()
    r = requests.request(
        method, BASE_URL + path, timeout=20,
        headers={"APCA-API-KEY-ID": KEY_ID, "APCA-API-SECRET-KEY": SECRET}, **kw)
    if r.status_code >= 400:
        raise MirrorError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
    return r.json() if r.text else None


# ----------------------------- state -----------------------------
def load_mstate():
    try:
        with open(MIRROR_STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"fills": [], "open_orders": [], "last_sync": None, "account": {}}


def save_mstate(m):
    m["fills"] = m["fills"][-500:]
    with open(MIRROR_STATE_FILE, "w") as f:
        json.dump(m, f, indent=2)


# ----------------------------- helpers -----------------------------
_asset_cache = {}


def asset(symbol):
    if symbol not in _asset_cache:
        _asset_cache[symbol] = _req("GET", f"/v2/assets/{symbol}")
    return _asset_cache[symbol]


def broker_positions():
    out = {}
    for p in _req("GET", "/v2/positions") or []:
        out[p["symbol"]] = float(p["qty"])  # negative for shorts
    return out


def open_order_symbols():
    return {o["symbol"] for o in (_req("GET", "/v2/orders", params={"status": "open"}) or [])}


def target_positions(sim_state):
    out = {}
    for sym, pos in sim_state["positions"].items():
        q = float(pos["qty"])
        out[sym] = q if pos["side"] == "LONG" else -q
    return out


def _resolve_fills(m):
    """Record actual fill prices for orders submitted on earlier runs."""
    still = []
    for o in m["open_orders"]:
        if not o.get("client_order_id"):
            m["fills"].append({**o, "status": "untracked"})
            continue
        try:
            r = _req("GET", "/v2/orders:by_client_order_id", params={"client_order_id": o["client_order_id"]})
        except MirrorError as e:
            log.warning(f"MIRROR lookup failed {o['client_order_id']}: {e}")
            still.append(o)
            continue
        status = r.get("status")
        if status == "filled":
            fp = float(r["filled_avg_price"])
            slip = (fp - o["sim_price"]) if o["side"] == "buy" else (o["sim_price"] - fp)
            rec = {**o, "status": "filled", "fill_price": fp, "filled_qty": float(r["filled_qty"]),
                   "filled_at": r.get("filled_at"), "slippage_per_share": round(slip, 4)}
            m["fills"].append(rec)
            log.info(f"MIRROR FILLED {o['side']} {o['symbol']} {rec['filled_qty']:.4f} @ {fp:.2f} "
                     f"(sim {o['sim_price']:.2f}, slippage/sh {slip:+.4f})")
        elif status in ("canceled", "expired", "rejected"):
            m["fills"].append({**o, "status": status})
            log.warning(f"MIRROR order {o['symbol']} {status}")
        else:
            still.append(o)
    m["open_orders"] = still


def _submit(m, symbol, side, qty, sim_price, reason):
    a = asset(symbol)
    if not a.get("tradable"):
        log.warning(f"MIRROR {symbol} not tradable on Alpaca; skipped")
        return
    if not a.get("fractionable"):
        qty = float(int(qty))
    qty = round(qty, 6)
    if qty <= QTY_EPS:
        return
    notional = qty * sim_price
    if notional > MAX_ORDER_USD:
        log.warning(f"MIRROR {side} {symbol} ${notional:.0f} exceeds MIRROR_MAX_ORDER_USD; skipped")
        return
    coid = f"pt-{uuid.uuid4().hex[:20]}"
    _req("POST", "/v2/orders", json={
        "symbol": symbol, "qty": str(qty), "side": side, "type": "market",
        "time_in_force": "day", "client_order_id": coid})
    m["open_orders"].append({
        "client_order_id": coid, "symbol": symbol, "side": side, "qty": qty,
        "sim_price": sim_price, "reason": reason,
        "submitted_at": datetime.now(timezone.utc).isoformat()})
    log.info(f"MIRROR SUBMIT {side} {symbol} {qty:.4f} (sim price {sim_price:.2f}, {reason})")


def sync(sim_state, prices):
    """Reconcile broker toward simulator. Safe to call every run."""
    if not enabled():
        return
    m = load_mstate()
    try:
        _guard()
        _resolve_fills(m)
        clock = _req("GET", "/v2/clock")
        acct = _req("GET", "/v2/account")
        m["account"] = {k: acct.get(k) for k in ("status", "equity", "cash", "buying_power")}
        m["account"]["mode"] = "live" if is_live() else "paper"
        if not clock.get("is_open"):
            return
        if acct.get("trading_blocked") or acct.get("account_blocked"):
            log.warning("MIRROR account blocked; no orders")
            return
        target = target_positions(sim_state)
        actual = broker_positions()
        busy = open_order_symbols()
        for sym in sorted(set(target) | set(actual)):
            if sym in busy:
                continue
            want, have = target.get(sym, 0.0), actual.get(sym, 0.0)
            if abs(want - have) <= QTY_EPS:
                continue
            px = prices.get(sym) or (sim_state["positions"].get(sym) or {}).get("entry")
            if not px:
                log.warning(f"MIRROR no price for {sym}; skipped")
                continue
            if abs(want) <= QTY_EPS and abs(have) > QTY_EPS:
                side = "sell" if have > 0 else "buy"
                last = next((t for t in reversed(sim_state.get("trades", [])) if t["symbol"] == sym), None)
                if last:
                    px = float(last["exit"])  # compare against the simulator's exit price
                r = _req("DELETE", f"/v2/positions/{sym}")  # flat close, handles fractional remainder
                m["open_orders"].append({
                    "client_order_id": (r or {}).get("client_order_id"), "symbol": sym, "side": side,
                    "qty": abs(have), "sim_price": px, "reason": "close",
                    "submitted_at": datetime.now(timezone.utc).isoformat()})
                log.info(f"MIRROR CLOSE {sym} {have:.4f} (sim price {px:.2f})")
            elif want < 0 or have < 0:
                log.warning(f"MIRROR {sym}: short positions not mirrored (want {want}, have {have})")
            elif want > have:
                _submit(m, sym, "buy", want - have, px, "open/increase")
            else:
                _submit(m, sym, "sell", have - want, px, "reduce")
    except Exception as e:  # never break the simulator
        log.warning(f"MIRROR error: {e}")
    finally:
        m["last_sync"] = datetime.now(timezone.utc).isoformat()
        save_mstate(m)


def check():
    """Verify keys and report readiness without trading. Prints no secrets."""
    print(f"endpoint: {'PAPER' if not is_live() else 'LIVE'} ({BASE_URL})")
    if not enabled():
        print("FAIL: ALPACA_KEY_ID / ALPACA_SECRET_KEY not set")
        return 1
    try:
        acct = _req("GET", "/v2/account")
        print(f"account status: {acct.get('status')}  equity: {acct.get('equity')}  "
              f"buying_power: {acct.get('buying_power')}  blocked: {acct.get('trading_blocked')}")
        print(f"market open now: {_req('GET', '/v2/clock').get('is_open')}")
        watch = os.environ.get("WATCHLIST") or "SPY,USO,UCO,XLE,UNG,GLD"
        for s in [x.strip().upper() for x in watch.split(",") if x.strip()]:
            a = asset(s)
            print(f"  {s}: tradable={a.get('tradable')} fractionable={a.get('fractionable')}")
        print("positions:", broker_positions() or "none")
        print("OK")
        return 0
    except MirrorError as e:
        print(f"FAIL: {e}")
        return 1


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(check())
    print("usage: broker_mirror.py --check")
