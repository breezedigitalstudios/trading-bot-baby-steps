"""
funnel.py — daily pipeline funnel snapshot.
Reads today's state files and appends one date-keyed entry to funnel.json.
"""

import json
import os
import re
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Dict, List

from telegram_alert import send_alert

BASE = os.path.dirname(__file__)

WATCHLIST_PATH = os.path.join(BASE, "watchlist.json")
SCORES_PATH    = os.path.join(BASE, "setup_scores.json")
TRADES_PATH    = os.path.join(BASE, "trades.json")
REGIME_PATH    = os.path.join(BASE, "regime.json")
FUNNEL_PATH    = os.path.join(BASE, "funnel.json")


# ── Skip reason normalization ──────────────────────────────────────────────────

_REASON_RULES = [
    (r"^regime_cash",              "regime_cash"),
    (r"^no ORB data",              "no_orb_data"),
    (r"^low opening volume",       "low_rvol"),
    (r"^ORB range .* > ATR",       "risk_too_wide"),
    (r"^breakout already occurred","already_broken_out"),
    (r"^earnings",                 "earnings_window"),
    (r"^sector concentration",     "sector_limit"),
    (r"^circuit_breaker",          "circuit_breaker"),
    (r"^already holding",          "already_holding"),
    (r"^max positions reached",    "max_positions"),
    (r"^max daily entries",        "max_daily_entries"),
    (r"^estimated cost .* exceeds","insufficient_capital"),
    (r"^position size computed",   "zero_shares"),
]

REASON_LABELS: Dict[str, str] = {
    "regime_cash":          "Regime: CASH",
    "no_orb_data":          "No ORB data",
    "low_rvol":             "Low RVOL",
    "risk_too_wide":        "Risk too wide (ORB > ATR)",
    "already_broken_out":   "Already broken out",
    "earnings_window":      "Earnings window",
    "sector_limit":         "Sector limit",
    "circuit_breaker":      "Circuit breaker",
    "already_holding":      "Already holding",
    "max_positions":        "Max positions",
    "max_daily_entries":    "Max daily entries",
    "insufficient_capital": "Insufficient capital",
    "zero_shares":          "Zero shares computed",
    "other":                "Other",
}

REASON_ORDER = list(REASON_LABELS.keys())


def normalize_reason(raw: str) -> str:
    for pattern, key in _REASON_RULES:
        if re.search(pattern, raw, re.IGNORECASE):
            return key
    return "other"


def _load(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def build_funnel(today: str) -> dict:
    watchlist = _load(WATCHLIST_PATH)
    scores    = _load(SCORES_PATH)
    trades    = _load(TRADES_PATH)
    regime    = _load(REGIME_PATH)

    # symbol → close lookup from setup_scores (used to enrich skipped/placed entries)
    close_by_sym: Dict[str, float] = {
        s["symbol"]: s.get("close")
        for s in scores.get("all_scored", [])
    }

    def sym_entry(symbol: str, close_override=None) -> dict:
        return {"symbol": symbol, "close": close_override or close_by_sym.get(symbol)}

    # Stage 1: scan pass
    candidates = watchlist.get("candidates", [])
    scan_pass = {
        "count":   len(candidates),
        "symbols": [{"symbol": c["symbol"], "close": c.get("close")} for c in candidates],
    }

    # Stage 2/3: setups by star rating
    high_quality = scores.get("high_quality", [])
    setups_4star = {
        "count":   sum(1 for s in high_quality if s["stars"] == 4),
        "symbols": [{"symbol": s["symbol"], "close": s.get("close")}
                    for s in high_quality if s["stars"] == 4],
    }
    setups_5star = {
        "count":   sum(1 for s in high_quality if s["stars"] == 5),
        "symbols": [{"symbol": s["symbol"], "close": s.get("close")}
                    for s in high_quality if s["stars"] == 5],
    }

    # Stage 4: skips today, grouped and deduplicated by reason bucket
    today_skipped = [s for s in trades.get("skipped", []) if s.get("date") == today]
    grouped: Dict[str, List[dict]] = defaultdict(list)
    for skip in today_skipped:
        grouped[normalize_reason(skip.get("reason", ""))].append(skip["symbol"])

    skipped: Dict[str, dict] = {}
    for key in REASON_ORDER:
        symbols = grouped.get(key, [])
        unique = list(dict.fromkeys(symbols))  # deduplicate preserving order
        if unique:
            skipped[key] = {"count": len(unique), "symbols": [sym_entry(s) for s in unique]}

    # Stage 5/6: orders placed and filled today
    today_trades = [t for t in trades.get("trades", []) if t.get("date") == today]
    orders_placed = {
        "count":   len(today_trades),
        "symbols": [sym_entry(t["symbol"]) for t in today_trades],
    }
    orders_filled = {
        "count":   sum(1 for t in today_trades if t.get("fill_price")),
        "symbols": [sym_entry(t["symbol"], t.get("fill_price"))
                    for t in today_trades if t.get("fill_price")],
    }

    return {
        "date":          today,
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "regime":        regime.get("regime", "UNKNOWN"),
        "regime_reason": regime.get("regime_reason", ""),
        "scan_pass":     scan_pass,
        "setups_4star":  setups_4star,
        "setups_5star":  setups_5star,
        "skipped":       skipped,
        "orders_placed": orders_placed,
        "orders_filled": orders_filled,
    }


def main():
    today = str(date.today())
    print(f"=== Daily Pipeline Funnel — {today} ===")

    funnel = _load(FUNNEL_PATH) if os.path.exists(FUNNEL_PATH) else {}
    entry  = build_funnel(today)
    funnel[today] = entry

    with open(FUNNEL_PATH, "w") as f:
        json.dump(funnel, f, indent=2)

    # Summary to stdout
    print(f"Regime:        {entry['regime']} [{entry['regime_reason']}]")
    print(f"Scan pass:     {entry['scan_pass']['count']} candidates")
    print(f"4★ setups:     {entry['setups_4star']['count']}")
    print(f"5★ setups:     {entry['setups_5star']['count']}")
    if entry["skipped"]:
        print("Skipped today:")
        for key, val in entry["skipped"].items():
            print(f"  {REASON_LABELS.get(key, key)}: {val['count']}")
    else:
        print("Skipped today: none")
    print(f"Orders placed: {entry['orders_placed']['count']}")
    print(f"Orders filled: {entry['orders_filled']['count']}")
    print(f"\nFunnel saved → funnel.json [{today}]")

    # Telegram summary
    regime      = entry["regime"]
    regime_icon = "🟢" if regime == "TRADE" else "🔴"
    reason      = entry["regime_reason"]

    def sym_list(syms, max_show=5):
        names = [s["symbol"] for s in syms]
        if not names:
            return "—"
        shown = "  ".join(names[:max_show])
        if len(names) > max_show:
            shown += f" +{len(names) - max_show} more"
        return shown

    lines = [
        f"<b>📊 Pipeline Funnel — {today}</b>",
        f"Regime: {regime_icon} <b>{regime}</b> [{reason}]",
        "",
        f"Scan pass:   {entry['scan_pass']['count']} candidates",
        f"  4★ setups: {entry['setups_4star']['count']}  {sym_list(entry['setups_4star']['symbols'])}",
        f"  5★ setups: {entry['setups_5star']['count']}  {sym_list(entry['setups_5star']['symbols'])}",
    ]

    if entry["skipped"]:
        lines.append("")
        lines.append("<b>Skipped:</b>")
        for key, val in entry["skipped"].items():
            label = REASON_LABELS.get(key, key)
            lines.append(f"  {label}: {val['count']}  {sym_list(val['symbols'])}")

    lines += [
        "",
        f"Orders placed: {entry['orders_placed']['count']}  {sym_list(entry['orders_placed']['symbols'])}",
        f"Orders filled: {entry['orders_filled']['count']}  {sym_list(entry['orders_filled']['symbols'])}",
    ]

    sent = send_alert("\n".join(lines))
    print(f"Telegram alert {'sent' if sent else 'skipped (no credentials)'}")


if __name__ == "__main__":
    main()
