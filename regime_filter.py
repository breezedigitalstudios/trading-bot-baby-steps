import json
import os
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from utils import save_json

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

API_KEY    = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
if not API_KEY or not SECRET_KEY:
    raise RuntimeError("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env")

data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
REGIME_PATH = os.path.join(os.path.dirname(__file__), "regime.json")


def check_regime() -> dict:
    """Check NASDAQ market regime. Return TRADE or CASH."""

    print("=== Market Regime Filter ===")
    print("[1/3] Fetching NASDAQ daily bars...")

    start = datetime.now(timezone.utc) - timedelta(days=100)
    req = StockBarsRequest(
        symbol_or_symbols="QQQ",
        timeframe=TimeFrame.Day,
        start=start,
    )

    try:
        bars = data_client.get_stock_bars(req).df
    except Exception as e:
        print(f"Error fetching QQQ data: {e}")
        return {"regime": "UNKNOWN", "error": str(e)}

    if bars.empty:
        print("No QQQ data returned.")
        return {"regime": "UNKNOWN", "error": "No data"}

    bars = bars.sort_index()
    close = bars["close"]

    print("[2/3] Calculating SMAs...")
    sma10 = close.rolling(10).mean()
    sma20 = close.rolling(20).mean()

    # Current values
    sma10_now = sma10.iloc[-1]
    sma20_now = sma20.iloc[-1]

    # Check SMA order
    above_20 = sma10_now > sma20_now

    # Check slope (is SMA increasing?)
    # Compare last value to 5 days ago
    sma10_slope = sma10.iloc[-1] - sma10.iloc[-6] if len(sma10) >= 6 else 0
    sma20_slope = sma20.iloc[-1] - sma20.iloc[-6] if len(sma20) >= 6 else 0

    sma10_up = sma10_slope > 0
    sma20_up = sma20_slope > 0

    # Regime decision
    print("[3/3] Evaluating regime...")
    regime = "TRADE" if (above_20 and sma10_up and sma20_up) else "CASH"

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "regime": regime,
        "qqq_close": round(float(close.iloc[-1]), 2),
        "sma10": round(float(sma10_now), 2),
        "sma20": round(float(sma20_now), 2),
        "sma10_above_sma20": bool(above_20),
        "sma10_slope_5d": round(float(sma10_slope), 2),
        "sma20_slope_5d": round(float(sma20_slope), 2),
        "sma10_sloping_up": bool(sma10_up),
        "sma20_sloping_up": bool(sma20_up),
    }

    print(f"\nQQQ Close:     ${result['qqq_close']}")
    print(f"SMA10:         ${result['sma10']}")
    print(f"SMA20:         ${result['sma20']}")
    print(f"10 > 20:       {'✓' if above_20 else '✗'}")
    print(f"SMA10 slope:   {result['sma10_slope_5d']:+.2f} ({'↑' if sma10_up else '↓'})")
    print(f"SMA20 slope:   {result['sma20_slope_5d']:+.2f} ({'↑' if sma20_up else '↓'})")
    print(f"\nRegime:        {regime} {'🟢' if regime == 'TRADE' else '🔴'}")

    return result


def main():
    result = check_regime()
    save_json(result, REGIME_PATH)
    print(f"\nRegime saved → regime.json")


if __name__ == "__main__":
    main()
