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
    order = get_order(trade["entry_order_id"])
    if order is None:
        return False

    if str(order.status) in ("expired", "canceled", "cancelled"):
        print(f"    Entry order expired/cancelled — skipping trade")
        trade.update({
            "status":      "expired",
            "exit_date":   str(date.today()),
            "exit_reason": "entry_order_expired",
        })
        return True

    if str(order.status) not in ("filled", "partially_filled"):
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

    if str(order.status) != "filled":
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

    # Sell 1/3
    sell_id = sell_market(trade["symbol"], shares_to_sell, "phase1_partial")
    if sell_id is None:
        return False

    phase1_pnl = round((current_price - fill_price) * shares_to_sell, 2)
    print(f"    Sold {shares_to_sell} shares @ ~${current_price:.2f}  partial PnL ${phase1_pnl:+.2f}")

    # Cancel old stop, place new stop at breakeven
    if trade.get("stop_order_id"):
        cancel_order(trade["stop_order_id"])

    new_stop_id = place_stop_loss(trade["symbol"], shares_after, fill_price)
    print(f"    Stop moved to breakeven ${fill_price:.2f}  ({shares_after} shares remaining)")

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

    fill_price = trade.get("fill_price", trade["orb_high"])
    pnl_runner = round((today_close - fill_price) * shares_remaining, 2)

    phase1_pnl = trade.get("phase1_pnl", 0.0) or 0.0
    total_pnl  = round(phase1_pnl + pnl_runner, 2)

    trade.update({
        "status":           "closed",
        "exit_price":       round(today_close, 2),
        "exit_date":        str(date.today()),
        "exit_reason":      "sma10_close",
        "sma10_at_exit":    round(sma10, 2),
        "pnl":              total_pnl,
    })
    print(f"    Exit order submitted  runner PnL ${pnl_runner:+.2f}  total PnL ${total_pnl:+.2f}")
    return True


# ── Main ───────────────────────────────────────────────────────────────────────

def run() -> None:
    print("=== Position Manager ===")
    now_et = datetime.now(ET)
    is_eod = now_et.hour >= 16
    print(f"Time (ET): {now_et.strftime('%H:%M:%S %Z')}  {'[EOD mode]' if is_eod else '[intraday mode]'}")

    trades, skipped = load_trades()
    active = [t for t in trades if t.get("status") in ("pending", "open", "partial_exit")]

    if not active:
        print("No active trades to manage.")
        return

    print(f"Active trades: {len(active)}")

    # Fetch current positions once
    positions = {p.symbol: p for p in trading_client.get_all_positions()}

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

        # ── Open / partial_exit ──

        # 1. Stop hit?
        if handle_stop_hit(trade):
            changed = True
            continue

        # 2. Position disappeared without a filled stop order → externally closed
        if symbol not in positions:
            print(f"    Position no longer exists — marking closed (external or manual)")
            trade.update({
                "status":     "closed",
                "exit_date":  str(date.today()),
                "exit_reason": "position_gone",
            })
            changed = True
            continue

        # 3. Phase 1 (only from 'open' status, not already partial)
        if status == "open":
            if handle_phase1(trade, positions):
                changed = True

        # 4. EOD SMA10 trailing exit
        if is_eod:
            if trade.get("status") in ("open", "partial_exit"):
                if handle_sma_exit(trade):
                    changed = True

    if changed:
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
