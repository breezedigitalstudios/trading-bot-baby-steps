"""
Entry Executor — run at or after 10:30 AM ET.

Flow:
  1. Gate checks: regime TRADE, open positions < 4, new entries today < 2
  2. For each high-quality setup:
       a. Skip if already positioned or ordered
       b. Fetch today's first 60-min candle (ORB)
       c. Compute 14-day ATR from daily bars
       d. Validate: ORB range <= ATR
       e. Size the position
       f. Place buy-stop order at ORB high (DAY order)
  3. Append all actions to trades.json
"""

import os
import json
import uuid
import pandas as pd
import pytz
from datetime import datetime, date, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest, StopOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

API_KEY    = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
if not API_KEY or not SECRET_KEY:
    raise RuntimeError("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env")

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client    = StockHistoricalDataClient(API_KEY, SECRET_KEY)

ET = pytz.timezone("America/New_York")

SCORES_PATH = os.path.join(os.path.dirname(__file__), "setup_scores.json")
REGIME_PATH = os.path.join(os.path.dirname(__file__), "regime.json")
TRADES_PATH = os.path.join(os.path.dirname(__file__), "trades.json")

MAX_POSITIONS    = 4
MAX_DAILY_ENTRIES = 2
ACCOUNT_RISK_PCT  = 0.10   # 10% of account risked per trade
MAX_POSITION_PCT  = 0.25   # hard cap: 25% of account per stock
CASH_BUFFER_PCT   = 0.25   # always keep 25% in cash
ATR_PERIOD        = 14
DRY_RUN           = False  # set True to log without placing orders


# ── Loaders ────────────────────────────────────────────────────────────────────

def load_setups() -> List[Dict]:
    if not os.path.exists(SCORES_PATH):
        raise FileNotFoundError("setup_scores.json not found — run setup_detector.py first")
    with open(SCORES_PATH) as f:
        return json.load(f).get("high_quality", [])


def load_regime() -> str:
    if not os.path.exists(REGIME_PATH):
        raise FileNotFoundError("regime.json not found — run regime_filter.py first")
    with open(REGIME_PATH) as f:
        return json.load(f).get("regime", "UNKNOWN")


def load_trades() -> Tuple[List[Dict], List[Dict]]:
    if not os.path.exists(TRADES_PATH):
        return [], []
    with open(TRADES_PATH) as f:
        payload = json.load(f)
    return payload.get("trades", []), payload.get("skipped", [])


def save_trades(trades: List[Dict], skipped: List[Dict]) -> None:
    with open(TRADES_PATH, "w") as f:
        json.dump({"trades": trades, "skipped": skipped}, f, indent=2, default=str)


def make_skip(symbol: str, stars: int, reason: str, **detail) -> Dict:
    record = {
        "id":        str(uuid.uuid4()),
        "date":      str(date.today()),
        "symbol":    symbol,
        "stars":     stars,
        "reason":    reason,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if detail:
        record["detail"] = detail
    return record


# ── Market data ────────────────────────────────────────────────────────────────

def fetch_orb(symbol: str) -> Optional[Tuple[float, float]]:
    """
    Fetch the first 60-min candle of today for symbol.
    Returns (orb_high, orb_low) or None if unavailable.
    """
    today_et   = datetime.now(ET).date()
    start_utc  = ET.localize(
        datetime(today_et.year, today_et.month, today_et.day, 9, 0)
    ).astimezone(timezone.utc)
    end_utc    = ET.localize(
        datetime(today_et.year, today_et.month, today_et.day, 11, 0)
    ).astimezone(timezone.utc)

    try:
        req  = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Hour,
            start=start_utc,
            end=end_utc,
        )
        bars = data_client.get_stock_bars(req).df
    except Exception as e:
        print(f"    Warning: could not fetch ORB for {symbol}: {e}")
        return None

    if bars.empty:
        return None

    # Take only the first bar of the day (the 9:30 AM candle)
    try:
        sym_bars = bars.loc[symbol].sort_index() if symbol in bars.index.get_level_values(0) else bars.sort_index()
    except Exception:
        sym_bars = bars.sort_index()

    if sym_bars.empty:
        return None

    first = sym_bars.iloc[0]
    return float(first["high"]), float(first["low"])


def fetch_atr(symbol: str) -> Optional[float]:
    """Compute 14-day ATR from recent daily bars."""
    start = datetime.now(timezone.utc) - timedelta(days=40)
    try:
        req  = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start,
        )
        bars = data_client.get_stock_bars(req).df
    except Exception as e:
        print(f"    Warning: could not fetch ATR data for {symbol}: {e}")
        return None

    if bars.empty:
        return None

    try:
        g = bars.loc[symbol].sort_index() if symbol in bars.index.get_level_values(0) else bars.sort_index()
    except Exception:
        g = bars.sort_index()

    if len(g) < ATR_PERIOD + 1:
        return None

    prev_close = g["close"].shift(1)
    tr = pd.concat([
        g["high"] - g["low"],
        (g["high"] - prev_close).abs(),
        (g["low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr = tr.rolling(ATR_PERIOD).mean().iloc[-1]
    return float(atr)


# ── Account state ──────────────────────────────────────────────────────────────

def get_account_state() -> Dict:
    acct           = trading_client.get_account()
    positions      = trading_client.get_all_positions()
    open_symbols   = {p.symbol for p in positions}

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    todays_orders = trading_client.get_orders(
        GetOrdersRequest(status=QueryOrderStatus.ALL, after=today_start, limit=100)
    )
    pending_symbols  = {o.symbol for o in todays_orders}
    entries_today    = sum(1 for o in todays_orders if o.side == OrderSide.BUY)

    portfolio_value  = float(acct.portfolio_value)
    cash             = float(acct.cash)
    max_deployable   = portfolio_value * (1 - CASH_BUFFER_PCT)
    deployed         = portfolio_value - cash
    available_to_deploy = max(0.0, max_deployable - deployed)

    return {
        "portfolio_value":     portfolio_value,
        "cash":                cash,
        "deployed":            deployed,
        "available_to_deploy": available_to_deploy,
        "open_positions":      len(positions),
        "open_symbols":        open_symbols,
        "pending_symbols":     pending_symbols,
        "entries_today":       entries_today,
    }


# ── Position sizing ────────────────────────────────────────────────────────────

def size_position(portfolio_value: float, orb_high: float, orb_low: float) -> int:
    risk_per_share = orb_high - orb_low
    if risk_per_share <= 0:
        return 0

    shares_by_risk = (portfolio_value * ACCOUNT_RISK_PCT) / risk_per_share
    shares_by_cap  = (portfolio_value * MAX_POSITION_PCT)  / orb_high
    return max(1, int(min(shares_by_risk, shares_by_cap)))


# ── Order placement ────────────────────────────────────────────────────────────

def place_entry_order(symbol: str, shares: int, orb_high: float) -> Tuple[Optional[str], Optional[str]]:
    """Place a DAY buy-stop order at orb_high. Returns (order_id, error_reason)."""
    if DRY_RUN:
        fake_id = f"dry-run-{uuid.uuid4().hex[:8]}"
        print(f"    [DRY RUN] Would place buy-stop {shares} {symbol} @ ${orb_high:.2f}")
        return fake_id, None

    try:
        order = trading_client.submit_order(
            StopOrderRequest(
                symbol=symbol,
                qty=shares,
                side=OrderSide.BUY,
                stop_price=round(orb_high, 2),
                time_in_force=TimeInForce.DAY,
            )
        )
        return str(order.id), None
    except Exception as e:
        import json as _json
        reason = str(e)
        try:
            err = _json.loads(str(e))
            if err.get("code") == 42210000:
                market = err.get("market_price", "?")
                reason = (f"breakout already occurred — "
                          f"market ${market} already above ORB high ${orb_high:.2f}")
            else:
                reason = err.get("message", reason)
        except Exception:
            pass
        print(f"    ERROR placing order for {symbol}: {reason}")
        return None, reason


# ── Main ───────────────────────────────────────────────────────────────────────

def run() -> None:
    print("=== Entry Executor ===")
    now_et = datetime.now(ET)
    print(f"Time (ET): {now_et.strftime('%H:%M:%S %Z')}")

    if now_et.hour < 10 or (now_et.hour == 10 and now_et.minute < 30):
        print("Market: ORB not yet complete (need 10:30 AM ET). Exiting.")
        return

    # 1. Regime gate
    regime = load_regime()
    print(f"Regime:    {regime}")
    trades, skipped = load_trades()
    new_trades: List[Dict] = []
    new_skips:  List[Dict] = []

    if regime != "TRADE":
        print("Regime is CASH — no new entries today.")
        setups = load_setups()
        for s in setups:
            new_skips.append(make_skip(s["symbol"], s["stars"], "regime_cash"))
        skipped.extend(new_skips)
        save_trades(trades, skipped)
        print(f"Logged {len(new_skips)} skips (regime_cash) → trades.json")
        return

    # 2. Load candidates
    setups = load_setups()
    if not setups:
        print("No high-quality setups found.")
        return
    print(f"Setups:    {len(setups)} high-quality candidates")

    # 3. Account state
    state = get_account_state()
    print(f"Positions: {state['open_positions']}/{MAX_POSITIONS} open")
    print(f"Entries:   {state['entries_today']}/{MAX_DAILY_ENTRIES} today")
    print(f"Capital:   ${state['portfolio_value']:,.2f} portfolio  "
          f"${state['available_to_deploy']:,.2f} deployable")

    for setup in setups:
        symbol = setup["symbol"]
        stars  = setup["stars"]

        print(f"\n  [{symbol}] ★{'★' * (stars-1)}")

        # Gate checks
        if state["open_positions"] + len(new_trades) >= MAX_POSITIONS:
            reason = f"max positions reached ({MAX_POSITIONS})"
            print(f"    Skip: {reason}")
            new_skips.append(make_skip(symbol, stars, reason))
            break
        if state["entries_today"] + len(new_trades) >= MAX_DAILY_ENTRIES:
            reason = f"max daily entries reached ({MAX_DAILY_ENTRIES})"
            print(f"    Skip: {reason}")
            new_skips.append(make_skip(symbol, stars, reason))
            break
        if symbol in state["open_symbols"]:
            reason = "already holding this stock"
            print(f"    Skip: {reason}")
            new_skips.append(make_skip(symbol, stars, reason))
            continue
        if symbol in state["pending_symbols"]:
            reason = "already have a pending order"
            print(f"    Skip: {reason}")
            new_skips.append(make_skip(symbol, stars, reason))
            continue

        # Skip if already have a pending trade for this symbol today in trades.json
        already_pending = any(
            t.get("symbol") == symbol and t.get("status") == "pending"
            and t.get("date") == str(date.today())
            for t in trades
        )
        if already_pending:
            reason = "already placed entry order today"
            print(f"    Skip: {reason}")
            new_skips.append(make_skip(symbol, stars, reason))
            continue

        # Fetch ORB
        orb = fetch_orb(symbol)
        if orb is None:
            reason = "no ORB data available (market closed or pre-10:30 AM)"
            print(f"    Skip: {reason}")
            new_skips.append(make_skip(symbol, stars, reason))
            continue
        orb_high, orb_low = orb
        orb_range = orb_high - orb_low
        print(f"    ORB: high=${orb_high:.2f}  low=${orb_low:.2f}  range=${orb_range:.2f}")

        # Fetch ATR
        atr = fetch_atr(symbol)
        if atr is None:
            reason = "could not compute ATR"
            print(f"    Skip: {reason}")
            new_skips.append(make_skip(symbol, stars, reason))
            continue
        print(f"    ATR: ${atr:.2f}")

        # Validate ORB range <= ATR
        if orb_range > atr:
            reason = f"ORB range ${orb_range:.2f} > ATR ${atr:.2f} — risk too wide"
            print(f"    Skip: {reason}")
            new_skips.append(make_skip(
                symbol, stars, reason,
                orb_high=round(orb_high, 2), orb_low=round(orb_low, 2),
                orb_range=round(orb_range, 2), atr=round(atr, 2),
            ))
            continue

        # Check available capital
        shares_est    = size_position(state["portfolio_value"], orb_high, orb_low)
        cost_estimate = orb_high * shares_est
        if cost_estimate > state["available_to_deploy"]:
            reason = (f"estimated cost ${cost_estimate:,.2f} exceeds "
                      f"available capital ${state['available_to_deploy']:,.2f}")
            print(f"    Skip: {reason}")
            new_skips.append(make_skip(
                symbol, stars, reason,
                cost_estimate=round(cost_estimate, 2),
                available=round(state["available_to_deploy"], 2),
            ))
            continue

        # Size position
        shares         = shares_est
        risk_per_share = orb_high - orb_low
        total_risk     = shares * risk_per_share
        print(f"    Size: {shares} shares  risk/share=${risk_per_share:.2f}  total risk=${total_risk:.2f}")

        # Place order
        order_id, order_error = place_entry_order(symbol, shares, orb_high)
        if order_id is None:
            reason = order_error or "order submission failed"
            new_skips.append(make_skip(
                symbol, stars, reason,
                orb_high=round(orb_high, 2), orb_low=round(orb_low, 2),
            ))
            continue

        trade = {
            "id":             str(uuid.uuid4()),
            "date":           str(date.today()),
            "symbol":         symbol,
            "stars":          stars,
            "orb_high":       round(orb_high, 2),
            "orb_low":        round(orb_low, 2),
            "orb_range":      round(orb_range, 2),
            "atr":            round(atr, 2),
            "shares":         shares,
            "entry_order_id": order_id,
            "stop_price":     round(orb_low, 2),
            "status":         "pending",
            "timestamp":      datetime.now(timezone.utc).isoformat(),
        }
        new_trades.append(trade)
        print(f"    ✓ Order placed: buy-stop {shares} @ ${orb_high:.2f}  stop @ ${orb_low:.2f}  [id: {order_id}]")

    trades.extend(new_trades)
    skipped.extend(new_skips)
    save_trades(trades, skipped)

    print(f"\nSummary: {len(new_trades)} order(s) placed, {len(new_skips)} skipped → trades.json")
    if DRY_RUN:
        print("(DRY RUN — no real orders submitted)")


if __name__ == "__main__":
    run()
