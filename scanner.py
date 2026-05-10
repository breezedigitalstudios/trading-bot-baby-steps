import os
import json
import time
import re
from typing import Dict, List, Set
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetAssetsRequest
from alpaca.trading.enums import AssetClass, AssetExchange, AssetStatus
from utils import save_json

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
if not API_KEY or not SECRET_KEY:
    raise RuntimeError("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env")

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

ADR_MIN_PCT = 4.0
PRICE_MIN = 20
DOLLAR_VOLUME_MIN = 20_000_000
TOP_PERCENTILE = 93
MAX_PER_SCAN = 50
BATCH_SIZE = 200
WATCHLIST_PATH = os.path.join(os.path.dirname(__file__), "watchlist.json")

VALID_SYMBOL = re.compile(r"^[A-Z]{1,5}$")
VALID_EXCHANGES = {AssetExchange.NYSE, AssetExchange.NASDAQ, AssetExchange.AMEX}


def get_universe() -> List[str]:
    assets = trading_client.get_all_assets(
        GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE)
    )
    return sorted(
        a.symbol for a in assets
        if a.tradable
        and a.exchange in VALID_EXCHANGES
        and VALID_SYMBOL.match(a.symbol)
    )


def fetch_all_bars(symbols: List[str]) -> pd.DataFrame:
    start = datetime.now(timezone.utc) - timedelta(days=200)
    frames = []
    n_batches = (len(symbols) + BATCH_SIZE - 1) // BATCH_SIZE
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i : i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        print(f"  Fetching batch {batch_num}/{n_batches} ({len(batch)} symbols)...")
        try:
            req = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame.Day,
                start=start,
            )
            df = data_client.get_stock_bars(req).df
            if not df.empty:
                frames.append(df)
        except Exception as e:
            print(f"  Warning: batch {batch_num} failed: {e}")
        time.sleep(0.3)
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames)
    return combined[~combined.index.duplicated(keep="last")]


def calculate_metrics(bars: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for symbol, group in bars.groupby(level=0):
        g = group.droplevel(0).sort_index()
        n = len(g)
        if n < 22:
            continue

        close = g["close"].iloc[-1]
        m22   = close / g["low"].iloc[-22:].min()
        m67   = close / g["low"].iloc[-67:].min()  if n >= 67  else np.nan
        m126  = close / g["low"].iloc[-126:].min() if n >= 126 else np.nan

        recent = g.iloc[-20:]
        adr  = ((recent["high"] - recent["low"]) / recent["close"]).mean() * 100
        dvol = (recent["close"] * recent["volume"]).mean()

        rows.append({
            "symbol":        symbol,
            "close":         round(float(close), 2),
            "momentum_22":   round(float(m22), 4),
            "momentum_67":   round(float(m67),  4) if pd.notna(m67)  else None,
            "momentum_126":  round(float(m126), 4) if pd.notna(m126) else None,
            "adr_pct":       round(float(adr), 2),
            "dollar_volume": round(float(dvol), 0),
        })
    return pd.DataFrame(rows).set_index("symbol")


def run_scan() -> List[Dict]:
    print("=== EOD Scanner ===")

    print("[1/4] Loading universe...")
    symbols = get_universe()
    print(f"      {len(symbols)} stocks")

    print("[2/4] Fetching daily bars (last 200 calendar days)...")
    bars = fetch_all_bars(symbols)
    if bars.empty:
        print("      No data returned. Exiting.")
        return []

    print("[3/4] Calculating metrics...")
    metrics = calculate_metrics(bars)
    print(f"      Metrics ready for {len(metrics)} stocks")

    print("[4/4] Running momentum scans...")
    scans = [
        ("1m",  "momentum_22"),
        ("3m",  "momentum_67"),
        ("6m",  "momentum_126"),
    ]

    watchlist_symbols: Set[str] = set()
    for label, col in scans:
        valid = metrics[metrics[col].notna()].copy()
        if valid.empty:
            continue
        threshold = valid[col].quantile(TOP_PERCENTILE / 100)
        top = (
            valid[
                (valid[col] >= threshold) &
                (valid["close"] >= PRICE_MIN) &
                (valid["adr_pct"] >= ADR_MIN_PCT) &
                (valid["dollar_volume"] >= DOLLAR_VOLUME_MIN)
            ]
            .nlargest(MAX_PER_SCAN, col)
        )
        watchlist_symbols.update(top.index.tolist())
        print(f"      {label}: {len(top)} candidates (threshold ≥ {threshold:.3f}×)")

    print(f"      {len(watchlist_symbols)} unique candidates across all scans")

    watchlist = (
        metrics.loc[metrics.index.isin(watchlist_symbols)]
        .reset_index()
        .sort_values("momentum_22", ascending=False, na_position="last")
        .to_dict(orient="records")
    )

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(watchlist),
        "candidates": watchlist,
    }
    save_json(payload, WATCHLIST_PATH)
    print(f"\nWatchlist saved → watchlist.json ({len(watchlist)} candidates)")

    return watchlist


if __name__ == "__main__":
    results = run_scan()
    if results:
        print("\nTop 10 candidates:")
        for c in results[:10]:
            print(
                f"  {c['symbol']:<6}  "
                f"close=${c['close']:>8.2f}  "
                f"adr={c['adr_pct']:>5.1f}%  "
                f"m22={c['momentum_22'] or 0:>6.3f}  "
                f"dvol=${c['dollar_volume'] / 1e6:>5.1f}M"
            )
