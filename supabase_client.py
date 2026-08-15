"""
supabase_client.py — shared Supabase helpers for all bot scripts.

Returns None from get_client() when credentials are absent so scripts
work locally without Supabase configured.
"""

import os
import re

try:
    from supabase import create_client, Client
    _SUPABASE_AVAILABLE = True
except ImportError:
    _SUPABASE_AVAILABLE = False

_client = None


def get_client():
    global _client
    if _client is not None:
        return _client
    if not _SUPABASE_AVAILABLE:
        return None
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        return None
    _client = create_client(url, key)
    return _client


def _clean(val):
    if isinstance(val, float) and (val != val or val == float("inf") or val == float("-inf")):
        return None
    return val


def sb_upsert(table: str, rows: list, conflict: str) -> None:
    """Upsert rows into table. Silently skips if no client or rows is empty."""
    if not rows:
        return
    sb = get_client()
    if sb is None:
        return
    try:
        cleaned = [{k: _clean(v) for k, v in row.items()} for row in rows]
        sb.table(table).upsert(cleaned, on_conflict=conflict).execute()
    except Exception as e:
        print(f"  [Supabase] {table} upsert failed: {e}")


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


def trade_row(t: dict) -> dict:
    return {
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
    }


def skip_row(s: dict) -> dict:
    detail = s.get("detail", {})
    return {
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
    }
