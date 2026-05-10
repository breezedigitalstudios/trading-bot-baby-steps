import os
import json
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from utils import save_json

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
if not API_KEY or not SECRET_KEY:
    raise RuntimeError("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env")

data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

WATCHLIST_PATH = os.path.join(os.path.dirname(__file__), "watchlist.json")
SCORES_PATH    = os.path.join(os.path.dirname(__file__), "setup_scores.json")
MIN_STARS = 4


def load_watchlist() -> List[Dict]:
    if not os.path.exists(WATCHLIST_PATH):
        raise FileNotFoundError("watchlist.json not found — run scanner.py first")
    with open(WATCHLIST_PATH) as f:
        return json.load(f)["candidates"]


def fetch_bars(symbols: List[str]) -> pd.DataFrame:
    # Include QQQ for relative strength; fetch ~90 calendar days for 60+ trading days
    all_syms = sorted(set(symbols + ["QQQ"]))
    start = datetime.now(timezone.utc) - timedelta(days=90)
    req = StockBarsRequest(
        symbol_or_symbols=all_syms,
        timeframe=TimeFrame.Day,
        start=start,
    )
    df = data_client.get_stock_bars(req).df
    return df[~df.index.duplicated(keep="last")]


def score_setup(g: pd.DataFrame, qqq_close: pd.Series) -> Tuple[int, Dict]:
    n = len(g)
    if n < 50:
        return 1, {"skip_reason": f"only {n} bars (need 50)"}

    close = g["close"]
    high  = g["high"]
    low   = g["low"]
    vol   = g["volume"]

    # 1. MA alignment: 10 SMA > 20 SMA > 50 SMA and price above 20 SMA
    sma10 = close.iloc[-10:].mean()
    sma20 = close.iloc[-20:].mean()
    sma50 = close.iloc[-50:].mean()
    ma_aligned = int(sma10 > sma20 > sma50 and close.iloc[-1] > sma20)

    # 2. Higher lows: recent 5-day floor is above prior 10-day floor
    recent_low = low.iloc[-5:].min()
    prior_low  = low.iloc[-15:-5].min()
    higher_lows = int(recent_low > prior_low)

    # 3. Range tightening: recent avg daily range < prior avg daily range
    recent_range = (high.iloc[-5:]   - low.iloc[-5:]).mean()
    prior_range  = (high.iloc[-15:-5] - low.iloc[-15:-5]).mean()
    tightening = int(recent_range < prior_range)

    # 4. Narrow-range candle: yesterday's range below median of past 20 days
    yesterday_range = high.iloc[-1] - low.iloc[-1]
    median_range    = (high.iloc[-20:] - low.iloc[-20:]).median()
    narrow_candle = int(yesterday_range < median_range * 0.75)

    # 5. Volume dry-up: recent avg volume ≥ 15% below prior avg volume
    recent_vol = vol.iloc[-5:].mean()
    prior_vol  = vol.iloc[-20:-5].mean()
    vol_dryup = int(recent_vol < prior_vol * 0.85)

    # Relative strength vs QQQ (last 10 days) — logged only, not scored
    rel_strength: Optional[bool] = None
    if len(qqq_close) >= 10:
        stock_ret = float(close.iloc[-1] / close.iloc[-10] - 1)
        mkt_ret   = float(qqq_close.iloc[-1] / qqq_close.iloc[-10] - 1)
        rel_strength = stock_ret > mkt_ret

    score = ma_aligned + higher_lows + tightening + narrow_candle + vol_dryup
    stars = max(1, min(5, score))

    breakdown = {
        "ma_aligned":       bool(ma_aligned),
        "higher_lows":      bool(higher_lows),
        "range_tightening": bool(tightening),
        "narrow_candle":    bool(narrow_candle),
        "volume_dryup":     bool(vol_dryup),
        "relative_strength_vs_qqq": rel_strength,
        "sma10": round(float(sma10), 2),
        "sma20": round(float(sma20), 2),
        "sma50": round(float(sma50), 2),
    }
    return stars, breakdown


def run_detector() -> List[Dict]:
    print("=== Setup Detector ===")

    print("[1/3] Loading watchlist...")
    candidates = load_watchlist()
    symbols = [c["symbol"] for c in candidates]
    print(f"      {len(symbols)} candidates")

    print("[2/3] Fetching daily bars...")
    bars = fetch_bars(symbols)

    # Extract QQQ close series
    try:
        qqq_close = bars.loc["QQQ"]["close"].sort_index()
    except KeyError:
        print("      Warning: QQQ data unavailable, skipping relative strength")
        qqq_close = pd.Series(dtype=float)

    print("[3/3] Scoring setups...")
    meta = {c["symbol"]: c for c in candidates}
    all_scored = []

    for symbol in symbols:
        try:
            g = bars.loc[symbol].sort_index() if symbol in bars.index.get_level_values(0) else pd.DataFrame()
        except Exception:
            g = pd.DataFrame()

        if g.empty:
            stars, breakdown = 1, {"skip_reason": "no bar data"}
        else:
            stars, breakdown = score_setup(g, qqq_close)

        entry = {
            "symbol":        symbol,
            "stars":         stars,
            "close":         meta[symbol].get("close"),
            "adr_pct":       meta[symbol].get("adr_pct"),
            "dollar_volume": meta[symbol].get("dollar_volume"),
            "momentum_22":   meta[symbol].get("momentum_22"),
            "momentum_67":   meta[symbol].get("momentum_67"),
            "momentum_126":  meta[symbol].get("momentum_126"),
            "breakdown":     breakdown,
        }
        all_scored.append(entry)

    all_scored.sort(key=lambda x: x["stars"], reverse=True)
    high_quality = [c for c in all_scored if c["stars"] >= MIN_STARS]

    payload = {
        "generated_at":      datetime.now(timezone.utc).isoformat(),
        "total_scored":      len(all_scored),
        "high_quality_count": len(high_quality),
        "high_quality":      high_quality,
        "all_scored":        all_scored,
    }
    save_json(payload, SCORES_PATH)

    print(f"\n{len(high_quality)} high-quality setups (≥{MIN_STARS} stars) → setup_scores.json")
    return high_quality


if __name__ == "__main__":
    results = run_detector()
    if results:
        print("\nHigh-quality setups:")
        for c in results:
            b = c["breakdown"]
            flags = "  ".join(
                k for k, v in b.items()
                if isinstance(v, bool) and v and k != "relative_strength_vs_qqq"
            )
            print(
                f"  {'★' * c['stars']}  {c['symbol']:<6}  "
                f"close=${c['close']:>8.2f}  adr={c['adr_pct']:>5.1f}%  "
                f"[{flags}]"
            )
    else:
        print("No setups scored ≥4 stars today.")
