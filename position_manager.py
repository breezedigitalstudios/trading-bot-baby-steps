"""
Position Manager — run during market hours and EOD.

Status flow:
  pending      → entry order placed, waiting for fill
  open         → entry filled, stop-loss active
  partial_exit → 1/3 sold (day 3+), stop moved to breakeven
  closed       → all shares exited

Run schedule:
  - Every 30-60 min during market hours  (checks fills + stop hits)
  - EOD after 4 PM ET                    (all of the above + SMA10 trailing exit)
"""

import os
import json
import uuid
import numpy as np
import pandas as pd
import pytz
from datetime import datetime, date, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    GetOrdersRequest, StopOrderRequest, MarketOrderRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus, OrderStatus
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

TRADES_PATH      = os.path.join(os.path.dirname(__file__), "trades.json")
PHASE1_DAYS      = 3    # trading days before phase 1 partial exit
PHASE1_SELL_FRAC = 1/3  # fraction to sell at phase 1
SMA_PERIOD       = 10   # trailing SMA for phase 2 exit
DRY_RUN          = False


# ── Persistence ────────────────────────────────────────────────────────────────

def load_trades() -> Tuple[List[Dict], List[Dict]]:
    if not os.path.exists(TRADES_PATH):
        return [], []
    with open(TRADES_PATH) as f:
        p = json.load(f)
    return p.get("trades", []), p.get("skipped", [])


def save_trades(trades: List[Dict], skipped: List[Dict]) -> None:
    with open(TRADES_PATH, "w") as f:
        json.dump({"trades": trades, "skipped": skipped}, f, indent=2, default=str)


# ── Market data helpers ────────────────────────────────────────────────────────

def fetch_daily_bars(symbol: str, days: int = 25) -> Optional[pd.DataFrame]:
    start = datetime.now(timezone.utc) - timedelta(days=days * 2)
    try:
        req  = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start)
        bars = data_client.get_stock_bars(req).df
    except Exception as e:
        print(f"    Warning: could not fetch daily bars for {symbol}: {e}")
        return None

    if bars.empty:
        return None

    try:
        g = bars.loc[symbol].sort_index()
    except KeyError:
        g = bars.sort_index()

    return g.iloc[-days:] if len(g) >= days else g


def compute_sma10(bars: pd.DataFrame) -> Optional[float]:
    if len(bars) < SMA_PERIOD:
        return None
    return float(bars["close"].rolling(SMA_PERIOD).mean().iloc[-1])


def trading_days_since(entry_date_str: str) -> int:
    """Number of weekday business days between entry date and today (exclusive)."""
    return int(np.busday_count(entry_date_str, str(date.today())))


# ── Order helpers ──────────────────────────────────────────────────────────────

def get_order(order_id: str):
    try:
        return trading_client.get_order_by_id(order_id)
    except Exception:
        return None


def place_stop_loss(symbol: str, shares: int, stop_price: float) -> Optional[str]:
    if DRY_RUN:
        oid = f"dry-sl-{uuid.uuid4().hex[:8]}"
        print(f"    [DRY RUN] Would place GTC stop-loss {shares} {symbol} @ ${stop_price:.2f}")
        return oid
    try:
        order = trading_client.submit_order(
            StopOrderRequest(
                symbol=symbol,
                qty=shares,
                side=OrderSide.SELL,
                stop_price=round(stop_price, 2),
                time_in_force=TimeInForce.GTC,
            )
        )
        return str(order.id)
    except Exception as e:
        print(f"    ERROR placing stop-loss for {symbol}: {e}")
        return None


def cancel_order(order_id: str) -> bool:
    if DRY_RUN:
        print(f"    [DRY RUN] Would cancel order {order_id}")
        return True
    try:
        trading_client.cancel_order_by_id(order_id)
        return True
    except Exception as e:
        print(f"    Warning: could not cancel order {order_id}: {e}")
        return False


def sell_market(symbol: str, shares: int, reason: str) -> Optional[str]:
    if DRY_RUN:
        oid = f"dry-sell-{uuid.uuid4().hex[:8]}"
        print(f"    [DRY RUN] Would sell {shares} {symbol} at market ({reason})")
        return oid
    try:
        order = trading_client.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                qty=shares,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
        )
        return str(order.id)
    except Exception as e:
        print(f"    ERROR selling {symbol}: {e}")
        return None


# ── Trade state handlers ───────────────────────────────────────────────────────

def handle_pending(trade: Dict) -> bool:
    """Check if entry order filled. If yes, place stop-loss and mark open."""
    # Always check Alpaca first — a prior-day order may have been filled before expiry
    is_stale = trade.get("date") and trade["date"] < str(date.today())
    order    = get_order(trade["entry_order_id"])

    if order is None:
        if is_stale:
            print(f"    Pending trade from {trade['date']} — order not found, marking expired")
        else:
            print(f"    Entry order not found on Alpaca — marking as expired")
        trade.update({
            "status":      "expired",
            "exit_date":   str(date.today()),
            "exit_reason": "entry_order_not_found",
        })
        return True

    if order.status in ("expired", "canceled", "cancelled"):
        if is_stale:
            print(f"    Pending trade from {trade['date']} — order expired/cancelled, marking expired")
        else:
            print(f"    Entry order expired/cancelled — skipping trade")
        trade.update({
            "status":      "expired",
            "exit_date":   str(date.today()),
            "exit_reason": "entry_order_expired",
        })
        return True

    if order.status not in ("filled", "partially_filled"):
        if is_stale:
            # Order still shows as active on Alpaca despite being from a prior day.
            # Mark expired rather than leaving it in limbo indefinitely.
            print(f"    Pending trade from {trade['date']} — order status={order.status}, marking expired")
            trade.update({
                "status":      "expired",
                "exit_date":   str(date.today()),
                "exit_reason": "entry_order_stale",
            })
            return True
        return False

    fill_price = float(order.filled_avg_price or trade["orb_high"])
    fill_date  = str(order.filled_at.date()) if order.filled_at else str(date.today())
    shares     = int(float(order.filled_qty))

    print(f"    Entry filled: {shares} shares @ ${fill_price:.2f} on {fill_date}")

    stop_order_id = place_stop_loss(trade["symbol"], shares, trade["stop_price"])

    trade.update({
        "status":           "open",
        "fill_price":       round(fill_price, 2),
        "fill_date":        fill_date,
        "shares_remaining": shares,
        "shares":           shares,
        "current_stop":     trade["stop_price"],
        "stop_order_id":    stop_order_id,
    })
    print(f"    Stop-loss placed @ ${trade['stop_price']:.2f}  [id: {stop_order_id}]")
    return True


def handle_stop_hit(trade: Dict) -> bool:
    """Return True if the stop-loss order has been filled."""
    stop_id = trade.get("stop_order_id")
    if not stop_id:
        return False

    order = get_order(stop_id)
    if order is None:
        return False

    if order.status != "filled":
        return False

    exit_price = float(order.filled_avg_price or trade["current_stop"])
    shares     = trade.get("shares_remaining", trade["shares"])
    pnl        = round((exit_price - trade["fill_price"]) * shares, 2)

    print(f"    Stop hit: sold {shares} shares @ ${exit_price:.2f}  PnL ${pnl:+.2f}")
    trade.update({
        "status":     "closed",
        "exit_price": round(exit_price, 2),
        "exit_date":  str(date.today()),
        "exit_reason": "stop_hit",
        "pnl":        pnl,
    })
    return True


def handle_phase1(trade: Dict, positions: Dict) -> bool:
    """
    If trade has been open >= 3 trading days and is profitable:
    sell 1/3, cancel existing stop, place new stop at breakeven.
    """
    fill_date = trade.get("fill_date")
    if not fill_date:
        return False

    days_held = trading_days_since(fill_date)
    if days_held < PHASE1_DAYS:
        return False

    # Confirm still in profit
    pos = positions.get(trade["symbol"])
    if pos is None:
        return False

    current_price = float(pos.current_price)
    fill_price    = trade["fill_price"]
    if current_price <= fill_price:
        print(f"    Phase 1 eligible (day {days_held}) but not profitable — holding")
        return False

    shares_remaining = trade.get("shares_remaining", trade["shares"])
    shares_to_sell   = max(1, int(shares_remaining * PHASE1_SELL_FRAC))
    shares_after     = shares_remaining - shares_to_sell

    print(f"    Phase 1: day {days_held}, price ${current_price:.2f} > entry ${fill_price:.2f}")

    # Cancel existing stop first so the partial sell isn't blocked
    if trade.get("stop_order_id"):
        cancel_order(trade["stop_order_id"])

    # Sell 1/3
    sell_id = sell_market(trade["symbol"], shares_to_sell, "phase1_partial")
    if sell_id is None:
        # Sell failed — re-place the stop we just cancelled so position stays protected
        new_stop_id = place_stop_loss(trade["symbol"], shares_remaining, trade.get("current_stop") or fill_price)
        trade["stop_order_id"] = new_stop_id
        return False

    phase1_pnl = round((current_price - fill_price) * shares_to_sell, 2)
    print(f"    Sold {shares_to_sell} shares @ ~${current_price:.2f}  partial PnL ${phase1_pnl:+.2f}")

    new_stop_id = place_stop_loss(trade["symbol"], shares_after, fill_price)
    if new_stop_id:
        print(f"    Stop moved to breakeven ${fill_price:.2f}  ({shares_after} shares remaining)")
    else:
        print(f"    ⚠ Breakeven stop placement FAILED for {trade['symbol']} — will retry next run")

    trade.update({
        "status":           "partial_exit",
        "shares_remaining": shares_after,
        "current_stop":     fill_price,
        "stop_order_id":    new_stop_id,
        "phase1_sell_id":   sell_id,
        "phase1_pnl":       phase1_pnl,
        "phase1_date":      str(date.today()),
    })
    return True


def handle_sma_exit(trade: Dict) -> bool:
    """EOD: exit remaining shares if today's close is below the 10-day SMA."""
    bars = fetch_daily_bars(trade["symbol"])
    if bars is None or len(bars) < SMA_PERIOD:
        print(f"    SMA check skipped: insufficient data")
        return False

    today_close = float(bars["close"].iloc[-1])
    sma10       = compute_sma10(bars)

    if sma10 is None:
        return False

    print(f"    SMA10 check: close ${today_close:.2f}  SMA10 ${sma10:.2f}")

    if today_close >= sma10:
        print(f"    Holding — price above SMA10")
        return False

    # Close below SMA10 — exit remaining
    shares_remaining = trade.get("shares_remaining", trade["shares"])
    print(f"    Close BELOW SMA10 — exiting {shares_remaining} remaining shares")

    if trade.get("stop_order_id"):
        cancel_order(trade["stop_order_id"])

    sell_id = sell_market(trade["symbol"], shares_remaining, "sma10_close")
    if sell_id is None:
        return False

    # Mark as pending fill — actual close happens when the DAY order fills
    trade.update({
        "status":           "sma_exit_pending",
        "sma_exit_sell_id": sell_id,
        "sma_exit_close":   round(today_close, 2),
        "sma10_at_exit":    round(sma10, 2),
        "stop_order_id":    None,
    })
    print(f"    Sell order submitted (DAY) — awaiting fill confirmation next run")
    return True


def handle_sma_exit_fill(trade: Dict) -> bool:
    """
    Check whether the SMA-exit DAY sell order has filled.
    If filled  → mark closed with actual fill price.
    If expired → re-submit the sell order.
    """
    sell_id = trade.get("sma_exit_sell_id")
    if not sell_id:
        return False

    order = get_order(sell_id)

    if order is None or order.status in ("expired", "canceled", "cancelled"):
        # Order gone — re-submit
        shares = trade.get("shares_remaining", trade["shares"])
        print(f"    SMA exit order expired/missing — re-submitting sell for {shares} shares")
        new_id = sell_market(trade["symbol"], shares, "sma10_close_retry")
        if new_id:
            trade["sma_exit_sell_id"] = new_id
        return bool(new_id)

    if order.status not in ("filled", "partially_filled"):
        print(f"    SMA exit order still pending ({order.status})")
        return False

    exit_price = float(order.filled_avg_price or trade.get("sma_exit_close", trade["fill_price"]))
    shares     = trade.get("shares_remaining", trade["shares"])
    fill_price = trade.get("fill_price", trade["orb_high"])
    pnl_runner = round((exit_price - fill_price) * shares, 2)
    phase1_pnl = trade.get("phase1_pnl", 0.0) or 0.0
    total_pnl  = round(phase1_pnl + pnl_runner, 2)

    print(f"    SMA exit filled: {shares} shares @ ${exit_price:.2f}  runner PnL ${pnl_runner:+.2f}  total PnL ${total_pnl:+.2f}")
    trade.update({
        "status":     "closed",
        "exit_price": round(exit_price, 2),
        "exit_date":  str(date.today()),
        "exit_reason": "sma10_close",
        "pnl":        total_pnl,
    })
    return True


# ── Stop-loss safety net ───────────────────────────────────────────────────────

_INACTIVE_ORDER_STATUSES = frozenset({
    "filled", "canceled", "cancelled", "expired", "replaced", "done_for_day"
})


def ensure_stop_loss(trade: Dict, positions: Dict) -> bool:
    """
    Verify the stop-loss order for an open trade is still active on Alpaca.
    - Missing or inactive + price above stop  → place a new stop-loss.
    - Missing or inactive + price at/below stop → close at market immediately.
    Returns True if the trade state changed.
    """
    symbol     = trade["symbol"]
    stop_price = float(trade.get("current_stop") or trade.get("stop_price") or 0)
    shares     = trade.get("shares_remaining", trade["shares"])

    if stop_price <= 0:
        return False

    # Check whether the existing stop order is still live
    stop_id = trade.get("stop_order_id")
    if stop_id:
        order = get_order(stop_id)
        if order is not None and order.status not in _INACTIVE_ORDER_STATUSES:
            return False  # stop is healthy

    # Stop is absent or no longer active
    pos = positions.get(symbol)
    if pos is None:
        return False

    current_price = float(pos.current_price)

    if current_price <= stop_price:
        print(f"    ⚠ Stop-loss missing and price ${current_price:.2f} ≤ stop ${stop_price:.2f} "
              f"— closing at market")
        sell_id = sell_market(symbol, shares, "stop_missed_close")
        if sell_id is None:
            return False
        fill_price = trade.get("fill_price", trade.get("orb_high", current_price))
        phase1_pnl = trade.get("phase1_pnl", 0.0) or 0.0
        runner_pnl = round((current_price - fill_price) * shares, 2)
        trade.update({
            "status":     "closed",
            "exit_price": round(current_price, 2),
            "exit_date":  str(date.today()),
            "exit_reason": "stop_missed_close",
            "pnl":        round(phase1_pnl + runner_pnl, 2),
        })
        return True
    else:
        print(f"    ⚠ Stop-loss missing — placing new stop at ${stop_price:.2f}")
        new_stop_id = place_stop_loss(symbol, shares, stop_price)
        if new_stop_id is None:
            return False
        trade["stop_order_id"] = new_stop_id
        return True


# ── Alpaca reconciliation ──────────────────────────────────────────────────────

def reconcile_alpaca(trades: List[Dict]) -> Dict:
    """
    Print a full snapshot of the live Alpaca account and flag anything that
    doesn't match trades.json.  Returns the live positions dict for reuse.
    """
    print("\n── Alpaca Account ────────────────────────────────────────────────────")
    try:
        acct = trading_client.get_account()
        print(f"  Portfolio value : ${float(acct.portfolio_value):>12,.2f}")
        print(f"  Cash            : ${float(acct.cash):>12,.2f}")
        print(f"  Buying power    : ${float(acct.buying_power):>12,.2f}")
    except Exception as e:
        print(f"  WARNING: could not fetch account — {e}")

    # Live positions
    try:
        raw_positions = trading_client.get_all_positions()
    except Exception as e:
        print(f"  WARNING: could not fetch positions — {e}")
        raw_positions = []

    tracked_open_symbols = {
        t["symbol"] for t in trades
        if t.get("status") in ("open", "partial_exit", "sma_exit_pending")
    }

    print(f"\n  Live positions  : {len(raw_positions)}")
    positions = {}
    for p in raw_positions:
        positions[p.symbol] = p
        unrlzd  = float(p.unrealized_pl)
        tracked = "✓" if p.symbol in tracked_open_symbols else "⚠  NOT IN TRADES.JSON"
        print(f"    {p.symbol:<7}  {int(float(p.qty)):>6} shares  "
              f"@ ${float(p.current_price):.2f}  unrlzd: ${unrlzd:+,.2f}  {tracked}")

    # Open orders
    try:
        open_orders = trading_client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=100)
        )
    except Exception as e:
        print(f"  WARNING: could not fetch open orders — {e}")
        open_orders = []

    tracked_pending_ids = {
        t["entry_order_id"] for t in trades if t.get("status") == "pending"
    }
    tracked_stop_ids = {
        t.get("stop_order_id") for t in trades
        if t.get("status") in ("open", "partial_exit") and t.get("stop_order_id")
    }

    print(f"\n  Open orders     : {len(open_orders)}")
    for o in open_orders:
        oid     = str(o.id)
        stop    = f"${float(o.stop_price):.2f}" if o.stop_price else "—"
        qty     = int(float(o.qty or 0))
        if oid in tracked_pending_ids:
            label = "✓ entry"
        elif oid in tracked_stop_ids:
            label = "✓ stop-loss"
        else:
            label = "⚠  NOT IN TRADES.JSON"
        print(f"    {o.symbol:<7}  {str(o.side):<14}  {qty:>6} shares  "
              f"stop={stop:<10}  {o.status}  {label}")

    print("──────────────────────────────────────────────────────────────────────\n")
    return positions


# ── Main ───────────────────────────────────────────────────────────────────────

def run() -> None:
    print("=== Position Manager ===")
    now_et = datetime.now(ET)
    is_eod = now_et.hour >= 16
    print(f"Time (ET): {now_et.strftime('%H:%M:%S %Z')}  {'[EOD mode]' if is_eod else '[intraday mode]'}")

    trades, skipped = load_trades()

    # Always audit full Alpaca state first
    positions = reconcile_alpaca(trades)

    active = [t for t in trades if t.get("status") in ("pending", "open", "partial_exit", "sma_exit_pending")]

    if not active:
        print("No active trades to manage.")
        return

    print(f"Active trades: {len(active)}")

    changed   = False
    for trade in active:
        symbol = trade["symbol"]
        status = trade["status"]
        print(f"\n  [{symbol}] status={status}  shares={trade.get('shares_remaining', trade['shares'])}")

        # ── Pending: check for fill ──
        if status == "pending":
            if handle_pending(trade):
                changed = True
            continue

        # ── SMA exit pending: waiting for DAY sell order to fill ──
        if status == "sma_exit_pending":
            if handle_sma_exit_fill(trade):
                changed = True
            continue

        # ── Open / partial_exit ──

        # 1. Stop hit?
        if handle_stop_hit(trade):
            changed = True
            continue

        # 2. Position disappeared without a filled stop order → externally closed
        if symbol not in positions:
            print(f"    Position no longer exists — marking closed (external or manual)")
            if trade.get("stop_order_id"):
                cancel_order(trade["stop_order_id"])
            trade.update({
                "status":     "closed",
                "exit_date":  str(date.today()),
                "exit_reason": "position_gone",
            })
            changed = True
            continue

        # 3. Ensure stop-loss is active (place or close if price already through stop)
        if ensure_stop_loss(trade, positions):
            changed = True
            if trade.get("status") == "closed":
                continue

        # 4. Phase 1 — intraday only (DAY sell orders won't fill after market close)
        if status == "open" and not is_eod:
            if handle_phase1(trade, positions):
                changed = True

        # 5. EOD SMA10 trailing exit
        if is_eod:
            if trade.get("status") in ("open", "partial_exit"):
                if handle_sma_exit(trade):
                    changed = True

    if changed:
        if DRY_RUN:
            print("\n[DRY RUN] trades.json NOT written (changes shown above)")
        else:
            save_trades(trades, skipped)
            print("\nTrades updated → trades.json")
    else:
        print("\nNo changes.")

    # Summary
    closed  = [t for t in trades if t.get("status") == "closed"]
    open_   = [t for t in trades if t.get("status") in ("open", "partial_exit")]
    pending = [t for t in trades if t.get("status") == "pending"]
    print(f"\nPortfolio: {len(open_)} open  {len(pending)} pending  {len(closed)} closed")
    if closed:
        total_pnl = sum(t.get("pnl") or 0 for t in closed)
        print(f"Realised P&L: ${total_pnl:+.2f}")
    if DRY_RUN:
        print("(DRY RUN — no real orders submitted)")


if __name__ == "__main__":
    run()
