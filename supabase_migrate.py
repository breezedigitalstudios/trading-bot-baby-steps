"""
supabase_migrate.py — one-off backfill of all historical JSON data into Supabase.

Run once:
    SUPABASE_URL=https://... SUPABASE_SERVICE_KEY=... python supabase_migrate.py

Safe to re-run: all inserts use upsert with conflict keys.
"""

import json
import os
import re
import sys
from datetime import date
from pathlib import Path

from supabase import create_client, Client

BASE        = Path(__file__).parent
ARCHIVE_DIR = BASE / "archive"

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("ERROR: set SUPABASE_URL and SUPABASE_SERVICE_KEY env vars before running.")
    sys.exit(1)

sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ── Reason normalizer (mirrors funnel.py) ─────────────────────────────────────

_REASON_RULES = [
    (r"^regime_cash",               "regime_cash"),
    (r"^no ORB data",               "no_orb_data"),
    (r"^low opening volume",        "low_rvol"),
    (r"^ORB range .* > ATR",        "risk_too_wide"),
    (r"^breakout already occurred", "already_broken_out"),
    (r"^earnings",                  "earnings_window"),
    (r"^sector concentration",      "sector_limit"),
    (r"^circuit_breaker",           "circuit_breaker"),
    (r"^already holding",           "already_holding"),
    (r"^max positions reached",     "max_positions"),
    (r"^max daily entries",         "max_daily_entries"),
    (r"^estimated cost .* exceeds", "insufficient_capital"),
    (r"^position size computed",    "zero_shares"),
    (r"^order submission failed",   "submission_failed"),
]

def normalize_reason(raw: str) -> str:
    for pattern, key in _REASON_RULES:
        if re.search(pattern, raw, re.IGNORECASE):
            return key
    return "other"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clean(val):
    """Convert NaN/inf floats to None so JSON serialization doesn't break."""
    if isinstance(val, float) and (val != val or val == float("inf") or val == float("-inf")):
        return None
    return val


def upsert(table: str, rows: list, conflict: str) -> int:
    if not rows:
        return 0
    cleaned = [{k: _clean(v) for k, v in row.items()} for row in rows]
    sb.table(table).upsert(cleaned, on_conflict=conflict).execute()
    return len(rows)


def load_json(path) -> dict:
    with open(path) as f:
        return json.load(f)


def date_from_filename(path: Path) -> str:
    """Extract YYYY-MM-DD from filenames like regime_2026-08-14.json"""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", path.stem)
    return m.group(1) if m else ""


# ── Regime ────────────────────────────────────────────────────────────────────

def migrate_regime():
    print("\n[1/5] Regime...")
    rows = []
    for path in sorted(ARCHIVE_DIR.glob("regime_*.json")):
        try:
            d = load_json(path)
            if "generated_at" not in d:
                continue
            rows.append({
                "generated_at":      d["generated_at"],
                "regime":            d.get("regime", "UNKNOWN"),
                "regime_reason":     d.get("regime_reason"),
                "qqq_close":         d.get("qqq_close"),
                "sma10":             d.get("sma10"),
                "sma20":             d.get("sma20"),
                "sma10_above_sma20": d.get("sma10_above_sma20"),
                "sma10_slope_5d":    d.get("sma10_slope_5d"),
                "sma20_slope_5d":    d.get("sma20_slope_5d"),
                "sma10_sloping_up":  d.get("sma10_sloping_up"),
                "sma20_sloping_up":  d.get("sma20_sloping_up"),
                "vix":               d.get("vix"),
                "vix_threshold":     d.get("vix_threshold"),
                "vix_elevated":      d.get("vix_elevated"),
            })
        except Exception as e:
            print(f"  Warning: {path.name}: {e}")
    n = upsert("regime", rows, "generated_at")
    print(f"  Upserted {n} regime records")


# ── Watchlist candidates ───────────────────────────────────────────────────────

def migrate_watchlist():
    print("\n[2/5] Watchlist candidates...")
    rows = []
    for path in sorted(ARCHIVE_DIR.glob("watchlist_*.json")):
        try:
            d        = load_json(path)
            scan_date = date_from_filename(path)
            gen_at   = d.get("generated_at")
            for c in d.get("candidates", []):
                rows.append({
                    "scan_date":    scan_date,
                    "generated_at": gen_at,
                    "symbol":       c["symbol"],
                    "close":        c.get("close"),
                    "momentum_22":  c.get("momentum_22"),
                    "momentum_67":  c.get("momentum_67"),
                    "momentum_126": c.get("momentum_126"),
                    "adr_pct":      c.get("adr_pct"),
                    "dollar_volume":c.get("dollar_volume"),
                })
        except Exception as e:
            print(f"  Warning: {path.name}: {e}")
    n = upsert("watchlist_candidates", rows, "scan_date,symbol")
    print(f"  Upserted {n} watchlist rows")


# ── Setup scores ───────────────────────────────────────────────────────────────

def migrate_setup_scores():
    print("\n[3/5] Setup scores...")
    rows = []
    for path in sorted(ARCHIVE_DIR.glob("setup_scores_*.json")):
        try:
            d          = load_json(path)
            score_date = date_from_filename(path)
            gen_at     = d.get("generated_at")
            for s in d.get("all_scored", []):
                b = s.get("breakdown", {})
                rows.append({
                    "score_date":       score_date,
                    "generated_at":     gen_at,
                    "symbol":           s["symbol"],
                    "stars":            s.get("stars"),
                    "close":            s.get("close"),
                    "adr_pct":          s.get("adr_pct"),
                    "dollar_volume":    s.get("dollar_volume"),
                    "momentum_22":      s.get("momentum_22"),
                    "momentum_67":      s.get("momentum_67"),
                    "momentum_126":     s.get("momentum_126"),
                    "ma_aligned":       b.get("ma_aligned"),
                    "higher_lows":      b.get("higher_lows"),
                    "range_tightening": b.get("range_tightening"),
                    "narrow_candle":    b.get("narrow_candle"),
                    "volume_dryup":     b.get("volume_dryup"),
                    "rs_vs_spy_1m":     b.get("rs_vs_spy_1m"),
                    "sma10":            b.get("sma10"),
                    "sma20":            b.get("sma20"),
                    "sma50":            b.get("sma50"),
                })
        except Exception as e:
            print(f"  Warning: {path.name}: {e}")
    n = upsert("setup_scores", rows, "score_date,symbol")
    print(f"  Upserted {n} setup score rows")


# ── Trades and skips ──────────────────────────────────────────────────────────

def migrate_trades():
    print("\n[4/5] Trades and skips...")
    path = BASE / "trades.json"
    if not path.exists():
        print("  trades.json not found, skipping")
        return

    d      = load_json(path)
    trades = d.get("trades", [])
    skips  = d.get("skipped", [])

    trade_rows = []
    for t in trades:
        trade_rows.append({
            "id":                     t["id"],
            "trade_date":             t.get("date"),
            "symbol":                 t.get("symbol"),
            "stars":                  t.get("stars"),
            "orb_high":               t.get("orb_high"),
            "orb_low":                t.get("orb_low"),
            "orb_range":              t.get("orb_range"),
            "atr":                    t.get("atr"),
            "shares":                 t.get("shares"),
            "entry_order_id":         t.get("entry_order_id"),
            "stop_order_id":          t.get("stop_order_id"),
            "stop_price":             t.get("stop_price"),
            "initial_risk_per_share": t.get("initial_risk_per_share"),
            "circuit_breaker_halved": t.get("circuit_breaker_halved"),
            "status":                 t.get("status"),
            "fill_price":             t.get("fill_price"),
            "fill_date":              t.get("fill_date"),
            "shares_remaining":       t.get("shares_remaining"),
            "current_stop":           t.get("current_stop"),
            "phase1_pnl":             t.get("phase1_pnl"),
            "phase1_date":            t.get("phase1_date"),
            "exit_price":             t.get("exit_price"),
            "exit_date":              t.get("exit_date"),
            "exit_reason":            t.get("exit_reason"),
            "pnl":                    t.get("pnl"),
            "r_multiple":             t.get("r_multiple"),
            "sma_exit_close":         t.get("sma_exit_close"),
            "sma10_at_exit":          t.get("sma10_at_exit"),
            "created_at":             t.get("timestamp"),
        })

    skip_rows = []
    for s in skips:
        detail = s.get("detail", {})
        skip_rows.append({
            "id":                s["id"],
            "skip_date":         s.get("date"),
            "symbol":            s.get("symbol"),
            "stars":             s.get("stars"),
            "reason":            s.get("reason"),
            "normalized_reason": normalize_reason(s.get("reason", "")),
            "rvol":              detail.get("rvol"),
            "atr":               detail.get("atr"),
            "orb_high":          detail.get("orb_high"),
            "orb_low":           detail.get("orb_low"),
            "rs_vs_spy_1m":      detail.get("rs_vs_spy_1m"),
            "earnings_date":     detail.get("earnings_date"),
            "stops_this_week":   detail.get("stops_this_week"),
            "price":             detail.get("price"),
            "created_at":        s.get("timestamp"),
        })

    n1 = upsert("trades", trade_rows, "id")
    n2 = upsert("skips",  skip_rows,  "id")
    print(f"  Upserted {n1} trades, {n2} skips")


# ── Funnel daily ──────────────────────────────────────────────────────────────

def migrate_funnel():
    print("\n[5/5] Funnel daily...")
    path = BASE / "funnel.json"
    if not path.exists():
        print("  funnel.json not found, skipping")
        return

    funnel = load_json(path)
    rows   = []
    for day, e in funnel.items():
        rows.append({
            "funnel_date":         day,
            "generated_at":        e.get("generated_at"),
            "regime":              e.get("regime"),
            "regime_reason":       e.get("regime_reason"),
            "scan_pass_count":     e.get("scan_pass", {}).get("count"),
            "setups_5star_count":  e.get("setups_5star", {}).get("count"),
            "setups_4star_count":  e.get("setups_4star", {}).get("count"),
            "orders_placed_count": e.get("orders_placed", {}).get("count"),
            "orders_filled_count": e.get("orders_filled", {}).get("count"),
            "unprocessed_count":   e.get("unprocessed", {}).get("count"),
        })

    n = upsert("funnel_daily", rows, "funnel_date")
    print(f"  Upserted {n} funnel rows")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== Supabase Backfill ===")
    print(f"Target: {SUPABASE_URL}")

    migrate_regime()
    migrate_watchlist()
    migrate_setup_scores()
    migrate_trades()
    migrate_funnel()

    print("\nDone.")
