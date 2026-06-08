"""
Q1 Analytics: Are we finding the right stocks?

Methodology:
  4-star setups  — entry = ORB high (first 60-min candle), only stocks that
                   actually crossed the ORB high after 10:30 AM ET on the scan day.
                   Deduplicated to first trigger per symbol.
  Watchlist      — entry = EOD close on scan day (broad scanner pool, not traded).
  QQQ            — entry = EOD close on each scan date (market baseline).

Forward return windows: 5 and 10 trading days.
"""

import os
import json
import glob
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytz
from typing import Optional
from dotenv import load_dotenv
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
if not API_KEY or not SECRET_KEY:
    raise RuntimeError("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env")

data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

ARCHIVE_DIR = os.path.join(os.path.dirname(__file__), "archive")
FORWARD_WINDOWS = [5, 10]
DAILY_BATCH_SIZE = 50
ET = pytz.timezone("America/New_York")


# ── Archive loaders ────────────────────────────────────────────────────────────

def load_archive_data(lookback_weeks: int = 4):
    cutoff = datetime.now(timezone.utc) - timedelta(weeks=lookback_weeks)

    setups_by_date = {}
    watchlist_by_date = {}

    for path in sorted(glob.glob(os.path.join(ARCHIVE_DIR, "setup_scores_*.json"))):
        date_str = path.split("setup_scores_")[1].replace(".json", "")
        if datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc) < cutoff:
            continue
        with open(path) as f:
            d = json.load(f)
        setups_by_date[date_str] = d.get("high_quality", [])

    for path in sorted(glob.glob(os.path.join(ARCHIVE_DIR, "watchlist_*.json"))):
        date_str = path.split("watchlist_")[1].replace(".json", "")
        if datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc) < cutoff:
            continue
        with open(path) as f:
            d = json.load(f)
        watchlist_by_date[date_str] = d.get("candidates", [])

    return setups_by_date, watchlist_by_date


# ── Intraday ORB fetch ─────────────────────────────────────────────────────────

def fetch_orb_and_trigger(symbols: list, scan_date: str) -> dict:
    """
    For each symbol on scan_date, fetch hourly bars for the full trading day.
    Returns dict: symbol -> {"orb_high", "orb_low", "triggered"}
      - orb_high / orb_low: from the first bar (9:30-10:30 AM ET)
      - triggered: True if any post-ORB bar has high >= orb_high
    """
    dt = datetime.strptime(scan_date, "%Y-%m-%d")
    # Fetch 9:00 AM to 4:30 PM ET to capture full day in hourly bars
    start_utc = ET.localize(datetime(dt.year, dt.month, dt.day, 9, 0)).astimezone(timezone.utc)
    end_utc   = ET.localize(datetime(dt.year, dt.month, dt.day, 16, 30)).astimezone(timezone.utc)

    result = {}
    for i in range(0, len(symbols), 30):  # smaller batch for intraday
        batch = symbols[i : i + 30]
        try:
            req = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame.Hour,
                start=start_utc,
                end=end_utc,
                feed="iex",
            )
            bars = data_client.get_stock_bars(req).df
        except Exception as e:
            print(f"  Warning: ORB fetch failed for {scan_date} batch: {e}")
            time.sleep(1)
            continue

        if bars.empty:
            continue

        df = bars if not isinstance(bars.index, pd.MultiIndex) else bars
        syms_in_bars = (
            df.index.get_level_values(0).unique().tolist()
            if isinstance(df.index, pd.MultiIndex)
            else [batch[0]] if len(batch) == 1 else []
        )

        for sym in syms_in_bars:
            try:
                sym_bars = df.xs(sym, level=0).sort_index() if isinstance(df.index, pd.MultiIndex) else df.sort_index()
            except Exception:
                continue

            if sym_bars.empty:
                continue

            # First bar is the ORB candle (9:30-10:30 AM)
            first = sym_bars.iloc[0]
            orb_high = float(first["high"])
            orb_low  = float(first["low"])

            # Post-ORB bars: any bar after the first
            post_orb = sym_bars.iloc[1:]
            triggered = (not post_orb.empty) and (float(post_orb["high"].max()) >= orb_high)

            result[sym] = {
                "orb_high":  orb_high,
                "orb_low":   orb_low,
                "triggered": triggered,
            }

        time.sleep(0.3)

    return result


# ── Daily bar fetch (for forward returns) ─────────────────────────────────────

def fetch_daily_prices(symbols: list, start: str, end: str) -> dict:
    """Fetch daily bars. Returns dict: symbol -> DataFrame with DatetimeIndex and 'close' column."""
    if not symbols:
        return {}

    start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt   = datetime.strptime(end,   "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)

    result = {}
    for i in range(0, len(symbols), DAILY_BATCH_SIZE):
        batch = symbols[i : i + DAILY_BATCH_SIZE]
        try:
            req = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame.Day,
                start=start_dt,
                end=end_dt,
                feed="iex",
            )
            bars = data_client.get_stock_bars(req).df
            if bars.empty:
                continue
            if isinstance(bars.index, pd.MultiIndex):
                for sym in bars.index.get_level_values(0).unique():
                    result[sym] = bars.xs(sym, level=0)[["close"]].copy()
            else:
                for sym in batch:
                    if sym in bars.columns:
                        result[sym] = bars[[sym]].rename(columns={sym: "close"})
        except Exception as e:
            print(f"  Warning: daily fetch error: {e}")
        time.sleep(0.3)

    return result


# ── Forward return helpers ─────────────────────────────────────────────────────

def get_close_on_or_after(price_df: pd.DataFrame, date_str: str) -> Optional[float]:
    target = pd.Timestamp(date_str, tz="UTC")
    candidates = price_df[price_df.index >= target]
    return float(candidates.iloc[0]["close"]) if not candidates.empty else None


def get_close_n_days_later(price_df: pd.DataFrame, date_str: str, n: int) -> Optional[float]:
    target = pd.Timestamp(date_str, tz="UTC")
    candidates = price_df[price_df.index >= target]
    return float(candidates.iloc[n]["close"]) if len(candidates) > n else None


# ── Core analysis ──────────────────────────────────────────────────────────────

def build_setup_records(setups_by_date: dict, daily_prices: dict, windows: list) -> list:
    """
    Build records for 4-star setups using ORB-triggered entries.

    Steps per date:
      1. Fetch ORB for all symbols on that date.
      2. Keep only symbols that triggered (price crossed ORB high post-10:30 AM).
      3. After processing all dates, deduplicate: keep only first trigger per symbol.
      4. Compute forward returns from ORB high.
    """
    # Track first_shortlisted_date for each symbol across all dates
    first_shortlisted: dict = {}
    for date_str in sorted(setups_by_date.keys()):
        for pick in setups_by_date[date_str]:
            sym = pick["symbol"]
            if sym not in first_shortlisted:
                first_shortlisted[sym] = date_str

    all_dates = sorted(setups_by_date.keys())
    raw_records = []  # all (symbol, date) pairs that triggered

    for date_str in all_dates:
        picks = setups_by_date[date_str]
        if not picks:
            continue

        symbols = [p["symbol"] for p in picks]
        stars_map = {p["symbol"]: p["stars"] for p in picks}

        print(f"  {date_str}: fetching ORB for {len(symbols)} symbols...")
        orb_results = fetch_orb_and_trigger(symbols, date_str)

        triggered_count = sum(1 for v in orb_results.values() if v["triggered"])
        print(f"    → {triggered_count}/{len(orb_results)} triggered the ORB breakout")

        for sym, orb in orb_results.items():
            if not orb["triggered"]:
                continue

            entry_price = orb["orb_high"]
            days_since_first = _trading_days_between(first_shortlisted[sym], date_str, all_dates)

            raw_records.append({
                "date":               date_str,
                "symbol":             sym,
                "stars":              stars_map.get(sym),
                "orb_high":           entry_price,
                "orb_low":            orb["orb_low"],
                "first_shortlisted":  first_shortlisted[sym],
                "days_since_first":   days_since_first,
                "entry_price":        entry_price,
            })

    # Deduplicate: first trigger date per symbol
    seen_symbols = set()
    deduped = []
    for r in sorted(raw_records, key=lambda x: x["date"]):
        if r["symbol"] not in seen_symbols:
            seen_symbols.add(r["symbol"])
            deduped.append(r)

    print(f"\n  Setup records: {len(raw_records)} triggered observations → {len(deduped)} after dedup")

    # Add forward returns
    for r in deduped:
        sym = r["symbol"]
        entry = r["entry_price"]
        if sym not in daily_prices:
            for w in windows:
                r[f"ret_{w}d"] = None
            continue
        pdf = daily_prices[sym]
        for w in windows:
            fwd = get_close_n_days_later(pdf, r["date"], w)
            r[f"ret_{w}d"] = (fwd / entry - 1) if fwd and entry else None

    return deduped


def build_watchlist_records(watchlist_by_date: dict, daily_prices: dict, windows: list) -> list:
    """Watchlist records using EOD close as entry (broad scanner pool comparison)."""
    seen = set()
    records = []
    for date_str in sorted(watchlist_by_date.keys()):
        for pick in watchlist_by_date[date_str]:
            sym = pick["symbol"]
            key = (date_str, sym)
            if key in seen:
                continue
            seen.add(key)
            if sym not in daily_prices:
                continue
            pdf = daily_prices[sym]
            entry = get_close_on_or_after(pdf, date_str)
            if entry is None:
                continue
            row = {"date": date_str, "symbol": sym, "entry_price": entry}
            for w in windows:
                fwd = get_close_n_days_later(pdf, date_str, w)
                row[f"ret_{w}d"] = (fwd / entry - 1) if fwd else None
            records.append(row)
    return records


def build_qqq_records(all_dates: list, daily_prices: dict, windows: list) -> list:
    records = []
    if "QQQ" not in daily_prices:
        return records
    pdf = daily_prices["QQQ"]
    for date_str in all_dates:
        entry = get_close_on_or_after(pdf, date_str)
        if entry is None:
            continue
        row = {"date": date_str, "symbol": "QQQ", "entry_price": entry}
        for w in windows:
            fwd = get_close_n_days_later(pdf, date_str, w)
            row[f"ret_{w}d"] = (fwd / entry - 1) if fwd else None
        records.append(row)
    return records


def _trading_days_between(from_date: str, to_date: str, all_dates: list) -> int:
    """Count scan days between first_shortlisted and trigger date (inclusive of from, exclusive of to)."""
    sorted_dates = sorted(all_dates)
    try:
        i_from = sorted_dates.index(from_date)
        i_to   = sorted_dates.index(to_date)
        return i_to - i_from
    except ValueError:
        return 0


# ── Stats & reporting ──────────────────────────────────────────────────────────

def summarize(records, label, windows):
    stats = {"label": label, "n_observations": len(records)}
    for w in windows:
        vals = [r[f"ret_{w}d"] for r in records if r.get(f"ret_{w}d") is not None]
        if not vals:
            for k in [f"{w}d_count", f"{w}d_avg_ret", f"{w}d_median_ret", f"{w}d_pct_positive"]:
                stats[k] = None
            continue
        stats[f"{w}d_count"]       = len(vals)
        stats[f"{w}d_avg_ret"]     = np.mean(vals)
        stats[f"{w}d_median_ret"]  = np.median(vals)
        stats[f"{w}d_pct_positive"] = np.mean([v > 0 for v in vals])
    return stats


def compute_alpha(group_records, qqq_records, window):
    qqq_by_date = {r["date"]: r.get(f"ret_{window}d") for r in qqq_records if r.get(f"ret_{window}d") is not None}
    alphas = []
    for r in group_records:
        ret = r.get(f"ret_{window}d")
        qqq = qqq_by_date.get(r["date"])
        if ret is not None and qqq is not None:
            alphas.append(ret - qqq)
    if not alphas:
        return {"count": 0, "avg_alpha": None, "median_alpha": None, "hit_rate_vs_qqq": None}
    return {
        "count":            len(alphas),
        "avg_alpha":        np.mean(alphas),
        "median_alpha":     np.median(alphas),
        "hit_rate_vs_qqq":  np.mean([a > 0 for a in alphas]),
    }


def p(val) -> str:
    return "N/A" if val is None else f"{val*100:+.1f}%"


def r(val) -> str:
    return "N/A" if val is None else f"{val*100:.0f}%"


def print_report(setup_records, watchlist_records, qqq_records, windows, date_range, lookback_weeks):
    setup_stats    = summarize(setup_records,     "4-star setups (ORB-triggered)", windows)
    watchlist_stats = summarize(watchlist_records, "Watchlist (EOD close)",         windows)
    qqq_stats      = summarize(qqq_records,       "QQQ (baseline)",                windows)

    setup_alpha    = {w: compute_alpha(setup_records,    qqq_records, w) for w in windows}
    watchlist_alpha = {w: compute_alpha(watchlist_records, qqq_records, w) for w in windows}

    lines = []
    lines.append("=" * 70)
    lines.append("  Q1 ANALYTICS: ARE WE FINDING THE RIGHT STOCKS?")
    lines.append(f"  Period  : {date_range[0]} → {date_range[1]}  (L{lookback_weeks}W)")
    lines.append(f"  Method  : 4-star setups use ORB-triggered entry (deduplicated).")
    lines.append(f"            Watchlist and QQQ use EOD close (comparison baseline).")
    lines.append("=" * 70)

    # Forward return table
    lines.append("\n── FORWARD RETURN SUMMARY ──────────────────────────────────────────")
    hdr = f"{'Group':<34} {'N':>4}"
    for w in windows:
        hdr += f"  {'Avg '+str(w)+'d':>8}  {'Med '+str(w)+'d':>8}  {'%Pos':>5}"
    lines.append(hdr)
    lines.append("-" * 70)
    for stats in [qqq_stats, watchlist_stats, setup_stats]:
        row = f"{stats['label']:<34} {stats['n_observations']:>4}"
        for w in windows:
            row += f"  {p(stats.get(f'{w}d_avg_ret')):>8}  {p(stats.get(f'{w}d_median_ret')):>8}  {r(stats.get(f'{w}d_pct_positive')):>5}"
        lines.append(row)

    # Alpha
    lines.append("\n── ALPHA vs QQQ (stock return − QQQ return, same start date) ───────")
    for w in windows:
        lines.append(f"\n  {w}-Day Window")
        for label, alpha in [("Watchlist (EOD)", watchlist_alpha[w]), ("4-star setups (ORB)", setup_alpha[w])]:
            a = alpha
            lines.append(
                f"    {label:<24}  avg α={p(a['avg_alpha'])}  "
                f"med α={p(a.get('median_alpha'))}  "
                f"beat QQQ={r(a['hit_rate_vs_qqq'])}  (n={a['count']})"
            )

    # Star breakdown
    by_stars = defaultdict(list)
    for r_ in setup_records:
        if r_.get("stars"):
            by_stars[r_["stars"]].append(r_)
    if by_stars:
        lines.append("\n── SETUP QUALITY: 4-STAR vs 5-STAR ────────────────────────────────")
        for stars in sorted(by_stars.keys()):
            recs = by_stars[stars]
            row = f"  {stars}-star  (n={len(recs):>2})"
            for w in windows:
                vals = [x[f"ret_{w}d"] for x in recs if x.get(f"ret_{w}d") is not None]
                if vals:
                    row += f"  {w}d: avg={p(np.mean(vals))} med={p(np.median(vals))} n={len(vals)}"
            lines.append(row)

    # Days-since-first-shortlisted breakdown
    lines.append("\n── DAYS ON WATCHLIST BEFORE TRIGGER ────────────────────────────────")
    lines.append("  (How many scan days did the stock appear before it finally broke out?)")
    day_buckets = defaultdict(list)
    for r_ in setup_records:
        d = r_.get("days_since_first", 0)
        bucket = "Same day" if d == 0 else f"Day {d}"
        day_buckets[bucket].append(r_)
    for bucket in sorted(day_buckets.keys()):
        recs = day_buckets[bucket]
        syms = ", ".join(r_["symbol"] for r_ in recs)
        row = f"  {bucket:<10}  n={len(recs)}  {syms}"
        for w in windows:
            vals = [x[f"ret_{w}d"] for x in recs if x.get(f"ret_{w}d") is not None]
            if vals:
                row += f"  [{w}d avg={p(np.mean(vals))}]"
        lines.append(row)

    # Per-stock detail
    lines.append("\n── TRIGGERED SETUP DETAIL ──────────────────────────────────────────")
    lines.append(f"  {'Symbol':<6}  {'Date':<12}  {'Stars':>5}  {'ORB High':>9}  {'5d Ret':>7}  {'10d Ret':>8}  {'First Seen'}")
    lines.append("  " + "-" * 65)
    for r_ in sorted(setup_records, key=lambda x: x["date"]):
        lines.append(
            f"  {r_['symbol']:<6}  {r_['date']:<12}  "
            f"{'★'*r_.get('stars',0):>5}  "
            f"${r_['entry_price']:>8.2f}  "
            f"{p(r_.get(f'ret_5d')):>7}  "
            f"{p(r_.get(f'ret_10d')):>8}  "
            f"{r_.get('first_shortlisted','')} (+{r_.get('days_since_first',0)}d)"
        )

    lines.append("\n" + "=" * 70)
    report = "\n".join(lines)
    print(report)
    return report


# ── Main ───────────────────────────────────────────────────────────────────────

def run(lookback_weeks: int = 4) -> str:
    print(f"\nLoading archive data (L{lookback_weeks}W)...")
    setups_by_date, watchlist_by_date = load_archive_data(lookback_weeks)

    all_dates = sorted(set(list(setups_by_date.keys()) + list(watchlist_by_date.keys())))
    if not all_dates:
        print("No data found in archive.")
        return ""

    date_range = (all_dates[0], all_dates[-1])
    print(f"Scan dates: {', '.join(all_dates)}")

    # Symbols we need daily prices for
    setup_syms     = {p["symbol"] for picks in setups_by_date.values() for p in picks}
    watchlist_syms = {p["symbol"] for picks in watchlist_by_date.values() for p in picks}
    all_syms = setup_syms | watchlist_syms | {"QQQ"}

    max_window   = max(FORWARD_WINDOWS)
    end_estimate = (datetime.strptime(all_dates[-1], "%Y-%m-%d") + timedelta(days=max_window * 2)).strftime("%Y-%m-%d")

    print(f"\nFetching daily prices for {len(all_syms)} symbols ({all_dates[0]} → {end_estimate})...")
    daily_prices = fetch_daily_prices(list(all_syms), all_dates[0], end_estimate)
    print(f"Daily prices fetched for {len(daily_prices)} symbols.")

    print("\nFetching intraday ORB data per date (this takes ~1 min)...")
    setup_records = build_setup_records(setups_by_date, daily_prices, FORWARD_WINDOWS)

    watchlist_records = build_watchlist_records(watchlist_by_date, daily_prices, FORWARD_WINDOWS)
    qqq_records       = build_qqq_records(all_dates, daily_prices, FORWARD_WINDOWS)

    report = print_report(setup_records, watchlist_records, qqq_records, FORWARD_WINDOWS, date_range, lookback_weeks)

    output = {
        "generated_at":   datetime.now(timezone.utc).isoformat(),
        "lookback_weeks": lookback_weeks,
        "date_range":     {"start": date_range[0], "end": date_range[1]},
        "setup_records":  setup_records,
        "qqq_stats":      summarize(qqq_records,       "QQQ",      FORWARD_WINDOWS),
        "watchlist_stats": summarize(watchlist_records, "Watchlist", FORWARD_WINDOWS),
        "setup_stats":    summarize(setup_records,     "Setups",    FORWARD_WINDOWS),
    }
    out_path = os.path.join(os.path.dirname(__file__), "analytics_q1_result.json")
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\nJSON saved → {out_path}")
    return report


if __name__ == "__main__":
    run(lookback_weeks=4)
