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
import yfinance as yf
from datetime import datetime, date, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from dotenv import load_dotenv
from telegram_alert import send_alert

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest, StopOrderRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus, OrderClass
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
ACCOUNT_RISK_PCT  = 0.05   # 5% of account risked per trade
MAX_POSITION_PCT  = 0.20   # hard cap: 20% of account per stock
CASH_BUFFER_PCT   = 0.25   # always keep 25% in cash
ATR_PERIOD        = 14
DRY_RUN           = False  # set True to log without placing orders


# ── Loaders ────────────────────────────────────────────────────────────────────

def validate_state_files(max_age_hours: int = 80) -> None:
    """Abort if setup_scores.json or regime.json are missing or older than max_age_hours.
    80h covers the Friday EOD → Monday entry gap (~65h) with buffer."""
    now = datetime.now(timezone.utc)
    files = {
        "setup_scores.json": SCORES_PATH,
        "regime.json":       REGIME_PATH,
    }
    for name, path in files.items():
        if not os.path.exists(path):
            raise RuntimeError(f"State file missing: {name} — run the EOD pipeline first")
        try:
            with open(path) as f:
                generated_at = json.load(f).get("generated_at")
            if not generated_at:
                raise ValueError("missing generated_at field")
            ts = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_hours = (now - ts).total_seconds() / 3600
            if age_hours > max_age_hours:
                raise RuntimeError(
                    f"Stale state file: {name} is {age_hours:.1f}h old (max {max_age_hours}h) — "
                    f"generated at {generated_at}"
                )
            print(f"  {name}: {age_hours:.1f}h old — OK")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"Could not validate {name}: {e}")


def load_setups() -> List[Dict]:
    if not os.path.exists(SCORES_PATH):
        raise FileNotFoundError("setup_scores.json not found — run setup_detector.py first")
    with open(SCORES_PATH) as f:
        return json.load(f).get("high_quality", [])


def load_regime() -> Tuple[str, str]:
    """Return (regime, regime_reason) from regime.json."""
    if not os.path.exists(REGIME_PATH):
        raise FileNotFoundError("regime.json not found — run regime_filter.py first")
    with open(REGIME_PATH) as f:
        data = json.load(f)
    return data.get("regime", "UNKNOWN"), data.get("regime_reason", "unknown")


def load_trades() -> Tuple[List[Dict], List[Dict]]:
    if not os.path.exists(TRADES_PATH):
        return [], []
    with open(TRADES_PATH) as f:
        payload = json.load(f)
    return payload.get("trades", []), payload.get("skipped", [])


def save_trades(trades: List[Dict], skipped: List[Dict]) -> None:
    with open(TRADES_PATH, "w") as f:
        json.dump({"trades": trades, "skipped": skipped}, f, indent=2, default=str)


def count_stops_this_week(trades: List[Dict]) -> int:
    """Count stop_hit exits recorded since Monday of the current week."""
    today  = date.today()
    monday = today - timedelta(days=today.weekday())  # weekday(): Mon=0, Sun=6
    count  = 0
    for t in trades:
        if t.get("exit_reason") != "stop_hit":
            continue
        exit_str = t.get("exit_date")
        if not exit_str:
            continue
        try:
            if date.fromisoformat(str(exit_str)) >= monday:
                count += 1
        except ValueError:
            continue
    return count


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
    return float(first["high"]), float(first["low"]), float(first["volume"])


def fetch_atr(symbol: str) -> Optional[Tuple[float, float]]:
    """Compute 14-day ATR and 20-day avg daily volume from recent daily bars.
    Returns (atr, avg_daily_volume) or None if unavailable."""
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

    atr = float(tr.rolling(ATR_PERIOD).mean().iloc[-1])
    avg_daily_volume = float(g["volume"].iloc[-20:].mean())
    return atr, avg_daily_volume


MAX_POSITIONS_PER_SECTOR = 2


def fetch_sector(symbol: str, cache: Dict) -> Optional[str]:
    """Return the sector string for symbol, using cache to avoid duplicate API calls.
    Fails open (returns None) so an unavailable sector never blocks an entry."""
    if symbol in cache:
        return cache[symbol]
    try:
        sector = yf.Ticker(symbol).info.get("sector") or None
    except Exception:
        sector = None
    cache[symbol] = sector
    return sector


def has_earnings_soon(symbol: str, days: int = 3) -> Optional[str]:
    """
    Return the upcoming earnings date string if it falls within `days` trading days,
    else None. Fails open on any data error so a yfinance outage never blocks entries.
    """
    try:
        cal = yf.Ticker(symbol).calendar
        if not cal:
            return None
        raw = cal.get('Earnings Date')
        if not raw:
            return None
        all_dates = raw if isinstance(raw, list) else [raw]
        all_dates = [d.date() if hasattr(d, 'date') else d for d in all_dates]
        upcoming = [d for d in all_dates if d >= date.today()]
        if not upcoming:
            return None
        nearest = min(upcoming)
        import numpy as np
        trading_days = int(np.busday_count(str(date.today()), str(nearest)))
        return str(nearest) if trading_days <= days else None
    except Exception as e:
        print(f"    Warning: could not check earnings for {symbol}: {e}")
        return None


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

def size_position(portfolio_value: float, entry_price: float, atr: float) -> int:
    if atr <= 0 or entry_price <= 0:
        return 0
    shares_by_risk = (portfolio_value * ACCOUNT_RISK_PCT) / atr
    shares_by_cap  = (portfolio_value * MAX_POSITION_PCT) / entry_price
    return max(1, int(min(shares_by_risk, shares_by_cap)))


# ── Order placement ────────────────────────────────────────────────────────────

def place_entry_order(symbol: str, shares: int, orb_high: float, stop_price: float) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Place a DAY buy-stop at orb_high with an OTO stop-loss at stop_price.
    Returns (entry_order_id, stop_order_id, error_reason).
    The stop-loss leg is held by Alpaca and activates automatically when the entry fills.
    """
    if DRY_RUN:
        entry_id = f"dry-run-{uuid.uuid4().hex[:8]}"
        stop_id  = f"dry-sl-{uuid.uuid4().hex[:8]}"
        print(f"    [DRY RUN] Would place OTO bracket: buy-stop {shares} {symbol} "
              f"@ ${orb_high:.2f} / stop-loss @ ${stop_price:.2f}")
        return entry_id, stop_id, None

    try:
        order = trading_client.submit_order(
            StopOrderRequest(
                symbol=symbol,
                qty=shares,
                side=OrderSide.BUY,
                stop_price=round(orb_high, 2),
                time_in_force=TimeInForce.DAY,
                order_class=OrderClass.OTO,
                stop_loss=StopLossRequest(stop_price=round(stop_price, 2)),
            )
        )
        # Extract child stop-loss order ID from bracket legs
        legs = order.legs or []
        stop_leg = next((leg for leg in legs if leg.side == OrderSide.SELL), None)
        stop_order_id = str(stop_leg.id) if stop_leg else None
        if stop_order_id is None:
            print(f"    Warning: could not extract stop-loss leg ID for {symbol} — will place on fill")
        return str(order.id), stop_order_id, None
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
        return None, None, reason


# ── Main ───────────────────────────────────────────────────────────────────────

def run() -> None:
    print("=== Entry Executor ===")
    now_et = datetime.now(ET)
    print(f"Time (ET): {now_et.strftime('%H:%M:%S %Z')}")

    # 0. State file freshness check — abort early if upstream pipeline failed
    print("Validating state files...")
    validate_state_files()

    if now_et.hour < 10 or (now_et.hour == 10 and now_et.minute < 30):
        print("Market: ORB not yet complete (need 10:30 AM ET). Exiting.")
        return

    # 1. Regime gate
    regime, regime_reason = load_regime()
    print(f"Regime:    {regime} [{regime_reason}]")
    trades, skipped = load_trades()
    new_trades:   List[Dict] = []
    new_skips:    List[Dict] = []
    sector_cache: Dict       = {}

    if regime != "TRADE":
        print("Regime is CASH — no new entries today.")
        setups = load_setups()
        skip_reason = f"regime_cash: {regime_reason}"
        for s in setups:
            new_skips.append(make_skip(s["symbol"], s["stars"], skip_reason))
        skipped.extend(new_skips)
        save_trades(trades, skipped)
        print(f"Logged {len(new_skips)} skips ({skip_reason}) → trades.json")
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

    # 4. Circuit breaker
    stops_this_week = count_stops_this_week(trades)
    if stops_this_week >= 3:
        print(f"\nCircuit breaker PAUSED: {stops_this_week} stop-outs this week (≥3) — no new entries.")
        for s in setups:
            new_skips.append(make_skip(s["symbol"], s["stars"],
                                       f"circuit_breaker_paused ({stops_this_week} stops this week)",
                                       stops_this_week=stops_this_week))
        skipped.extend(new_skips)
        save_trades(trades, skipped)
        send_alert(
            f"🛑 <b>CIRCUIT BREAKER — PAUSED</b>\n"
            f"{stops_this_week} stop-outs this week — no new entries until Monday"
        )
        return
    elif stops_this_week == 2:
        size_multiplier = 0.5
        print(f"\nCircuit breaker HALF-SIZE: {stops_this_week} stop-outs this week — position sizes halved.")
        send_alert(
            f"⚠️ <b>CIRCUIT BREAKER — HALF SIZE</b>\n"
            f"{stops_this_week} stop-outs this week — new positions sized at 50%"
        )
    else:
        size_multiplier = 1.0

    for setup in setups:
        symbol = setup["symbol"]
        stars  = setup["stars"]

        print(f"\n  [{symbol}] ★{'★' * (stars-1)}")

        # Gate checks (continue — not break — so all remaining symbols are still logged)
        if state["open_positions"] + len(new_trades) >= MAX_POSITIONS:
            reason = f"max positions reached ({MAX_POSITIONS})"
            print(f"    Skip: {reason}")
            new_skips.append(make_skip(symbol, stars, reason))
            continue
        if state["entries_today"] + len(new_trades) >= MAX_DAILY_ENTRIES:
            reason = f"max daily entries reached ({MAX_DAILY_ENTRIES})"
            print(f"    Skip: {reason}")
            new_skips.append(make_skip(symbol, stars, reason))
            continue
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
        orb_high, orb_low, orb_volume = orb
        orb_range = orb_high - orb_low
        print(f"    ORB: high=${orb_high:.2f}  low=${orb_low:.2f}  range=${orb_range:.2f}")

        # Fetch ATR + avg daily volume
        atr_result = fetch_atr(symbol)
        if atr_result is None:
            reason = "could not compute ATR"
            print(f"    Skip: {reason}")
            new_skips.append(make_skip(symbol, stars, reason))
            continue
        atr, avg_daily_volume = atr_result
        print(f"    ATR: ${atr:.2f}")

        # Relative volume gate: opening-hour volume must be ≥1.5× average hourly volume
        avg_hourly_volume = avg_daily_volume / 6.5
        rvol = round(orb_volume / avg_hourly_volume, 2) if avg_hourly_volume > 0 else None
        if rvol is not None and rvol < 1.5:
            reason = f"low opening volume (RVOL={rvol:.2f}x, need ≥1.5x)"
            print(f"    Skip: {reason}")
            new_skips.append(make_skip(symbol, stars, reason,
                                       rvol=rvol,
                                       orb_volume=int(orb_volume),
                                       avg_hourly_volume=round(avg_hourly_volume, 0)))
            continue
        print(f"    RVOL: {rvol:.2f}x" if rvol is not None else "    RVOL: n/a")

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

        # Skip if stock is underperforming SPY over the last month
        rs = setup.get("breakdown", {}).get("rs_vs_spy_1m")
        if rs is not None and rs <= 1.0:
            reason = f"relative strength below market (RS={rs:.2f} vs SPY 1-month)"
            print(f"    Skip: {reason}")
            new_skips.append(make_skip(symbol, stars, reason, rs_vs_spy_1m=rs))
            continue

        # Skip if earnings are within 7 trading days
        earnings_date = has_earnings_soon(symbol, days=7)
        if earnings_date:
            reason = f"earnings within 7 trading days ({earnings_date})"
            print(f"    Skip: {reason}")
            new_skips.append(make_skip(symbol, stars, reason, earnings_date=earnings_date))
            continue

        # Sector concentration gate
        sector = fetch_sector(symbol, sector_cache)
        if sector:
            open_statuses = {"pending", "open", "partial_exit", "sma_exit_pending"}
            sector_count = sum(
                1 for t in trades + new_trades
                if t.get("status") in open_statuses
                and fetch_sector(t["symbol"], sector_cache) == sector
            )
            if sector_count >= MAX_POSITIONS_PER_SECTOR:
                reason = f"sector concentration ({sector}: already {sector_count} positions)"
                print(f"    Skip: {reason}")
                new_skips.append(make_skip(symbol, stars, reason,
                                           sector=sector, sector_count=sector_count))
                continue
            print(f"    Sector: {sector} ({sector_count}/{MAX_POSITIONS_PER_SECTOR})")
        else:
            print(f"    Sector: unknown — skipping concentration check")

        # Check available capital (apply circuit-breaker size multiplier before cost check)
        shares_est = size_position(state["portfolio_value"], orb_high, atr)
        if shares_est == 0:
            reason = f"position size computed as 0 (ATR=${atr:.2f}, price=${orb_high:.2f})"
            print(f"    Skip: {reason}")
            new_skips.append(make_skip(symbol, stars, reason, atr=round(atr, 2), price=round(orb_high, 2)))
            continue
        cb_halved = False
        if size_multiplier < 1.0:
            shares_est = max(1, int(shares_est * size_multiplier))
            cb_halved  = True
            print(f"    Circuit breaker: size reduced to {shares_est} shares (×{size_multiplier})")
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

        # Size position (risk_per_share = ATR, stop placed at orb_low)
        shares         = shares_est
        risk_per_share = atr
        total_risk     = shares * risk_per_share
        print(f"    Size: {shares} shares  ATR=${risk_per_share:.2f}  total risk=${total_risk:.2f}")

        # Place OTO bracket order (entry + stop-loss in one atomic submission)
        order_id, stop_order_id, order_error = place_entry_order(symbol, shares, orb_high, orb_low)
        if order_id is None:
            reason = order_error or "order submission failed"
            new_skips.append(make_skip(
                symbol, stars, reason,
                orb_high=round(orb_high, 2), orb_low=round(orb_low, 2),
            ))
            continue

        trade = {
            "id":                    str(uuid.uuid4()),
            "date":                  str(date.today()),
            "symbol":                symbol,
            "stars":                 stars,
            "orb_high":              round(orb_high, 2),
            "orb_low":               round(orb_low, 2),
            "orb_range":             round(orb_range, 2),
            "atr":                   round(atr, 2),
            "shares":                shares,
            "entry_order_id":        order_id,
            "stop_order_id":         stop_order_id,
            "stop_price":            round(orb_low, 2),
            "initial_risk_per_share": round(risk_per_share, 2),
            "circuit_breaker_halved": cb_halved,
            "status":                "pending",
            "timestamp":             datetime.now(timezone.utc).isoformat(),
        }
        new_trades.append(trade)
        stop_info = f"stop-loss @ ${orb_low:.2f}" if stop_order_id else "WARNING: stop-loss leg missing"
        print(f"    ✓ OTO bracket placed: buy-stop {shares} @ ${orb_high:.2f}  {stop_info}  [entry: {order_id}]")
        send_alert(
            f"🟢 <b>ENTRY PLACED</b>\n"
            f"{symbol}: buy-stop {shares} shares @ ${orb_high:.2f}\n"
            f"Stop-loss: ${orb_low:.2f} (ORB low)  |  Risk/share: ${orb_high - orb_low:.2f}"
        )

    trades.extend(new_trades)
    skipped.extend(new_skips)
    save_trades(trades, skipped)

    print(f"\nSummary: {len(new_trades)} order(s) placed, {len(new_skips)} skipped → trades.json")
    if DRY_RUN:
        print("(DRY RUN — no real orders submitted)")


if __name__ == "__main__":
    run()
