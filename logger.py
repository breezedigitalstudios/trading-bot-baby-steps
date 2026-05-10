"""
Logger — daily P&L report.
Reads trades.json + live positions and prints a full snapshot.
Saves a dated copy to archive/report_YYYY-MM-DD.txt.
"""

import os
import json
import pytz
from datetime import datetime, date, timezone
from typing import Dict, List, Tuple, Optional
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

API_KEY    = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
if not API_KEY or not SECRET_KEY:
    raise RuntimeError("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env")

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)

ET           = pytz.timezone("America/New_York")
TRADES_PATH  = os.path.join(os.path.dirname(__file__), "trades.json")
ARCHIVE_DIR  = os.path.join(os.path.dirname(__file__), "archive")


# ── Loaders ────────────────────────────────────────────────────────────────────

def load_trades() -> Tuple[List[Dict], List[Dict]]:
    if not os.path.exists(TRADES_PATH):
        return [], []
    with open(TRADES_PATH) as f:
        p = json.load(f)
    return p.get("trades", []), p.get("skipped", [])


def get_live_positions() -> Dict[str, Dict]:
    """Return dict of symbol → {current_price, qty, unrealized_pl}."""
    try:
        positions = trading_client.get_all_positions()
        return {
            p.symbol: {
                "current_price":  float(p.current_price),
                "qty":            int(float(p.qty)),
                "unrealized_pl":  float(p.unrealized_pl),
                "avg_entry_price": float(p.avg_entry_price),
            }
            for p in positions
        }
    except Exception as e:
        print(f"Warning: could not fetch live positions: {e}")
        return {}


def get_account() -> Dict:
    try:
        a = trading_client.get_account()
        return {
            "portfolio_value": float(a.portfolio_value),
            "cash":            float(a.cash),
            "buying_power":    float(a.buying_power),
        }
    except Exception as e:
        print(f"Warning: could not fetch account: {e}")
        return {}


# ── Formatting helpers ─────────────────────────────────────────────────────────

def stars(n: int) -> str:
    return "★" * n + "☆" * (5 - n)


def pnl_str(val: Optional[float]) -> str:
    if val is None:
        return "—"
    return f"${val:+,.2f}"


def divider(char: str = "─", width: int = 64) -> str:
    return char * width


# ── Report sections ────────────────────────────────────────────────────────────

def section_account(account: Dict) -> str:
    if not account:
        return ""
    lines = [
        "ACCOUNT",
        divider(),
        f"  Portfolio value  ${account['portfolio_value']:>12,.2f}",
        f"  Cash             ${account['cash']:>12,.2f}",
        f"  Buying power     ${account['buying_power']:>12,.2f}",
    ]
    return "\n".join(lines)


def section_open(trades: List[Dict], live: Dict[str, Dict]) -> str:
    open_trades = [t for t in trades if t.get("status") in ("open", "partial_exit")]
    if not open_trades:
        return "OPEN POSITIONS\n" + divider() + "\n  None."

    header = f"  {'Symbol':<7} {'Entry':>8} {'Current':>8} {'Unrlzd':>9} {'Shares':>7} {'Stop':>8} {'Days':>5} {'Phase'}"
    rows = ["OPEN POSITIONS", divider(), header, "  " + divider("·", 62)]

    for t in open_trades:
        sym   = t["symbol"]
        entry = t.get("fill_price") or t["orb_high"]
        pos   = live.get(sym, {})
        curr  = pos.get("current_price", entry)
        unrlzd = pos.get("unrealized_pl")
        shares = t.get("shares_remaining", t["shares"])
        stop   = t.get("current_stop", t["stop_price"])
        phase  = "partial" if t["status"] == "partial_exit" else "trailing"

        from datetime import date as _date
        import numpy as np
        fill_date = t.get("fill_date", str(_date.today()))
        days = int(np.busday_count(fill_date, str(_date.today())))

        rows.append(
            f"  {sym:<7} ${entry:>7.2f} ${curr:>7.2f} {pnl_str(unrlzd):>9} "
            f"{shares:>7} ${stop:>7.2f} {days:>5}  {phase}"
        )
    return "\n".join(rows)


def section_closed(trades: List[Dict], today_only: bool = False) -> str:
    closed = [t for t in trades if t.get("status") == "closed"]
    if today_only:
        closed = [t for t in closed if t.get("exit_date") == str(date.today())]

    title = f"CLOSED TODAY ({len(closed)})" if today_only else f"ALL CLOSED TRADES ({len(closed)})"
    if not closed:
        return title + "\n" + divider() + "\n  None."

    header = f"  {'Symbol':<7} {'Date':<12} {'Entry':>8} {'Exit':>8} {'P&L':>9} {'Stars':<7} {'Reason'}"
    rows   = [title, divider(), header, "  " + divider("·", 62)]

    for t in sorted(closed, key=lambda x: x.get("exit_date", ""), reverse=True):
        sym    = t["symbol"]
        edate  = t.get("exit_date", "?")
        entry  = t.get("fill_price") or t.get("orb_high", 0)
        exit_p = t.get("exit_price", 0) or 0
        pnl    = t.get("pnl")
        reason = t.get("exit_reason", "?")
        st     = stars(t.get("stars", 0))
        rows.append(
            f"  {sym:<7} {edate:<12} ${entry:>7.2f} ${exit_p:>7.2f} "
            f"{pnl_str(pnl):>9}  {st}  {reason}"
        )
    return "\n".join(rows)


def section_summary(trades: List[Dict], live: Dict[str, Dict]) -> str:
    closed   = [t for t in trades if t.get("status") == "closed"]
    today_cl = [t for t in closed if t.get("exit_date") == str(date.today())]

    realised_today = sum(t.get("pnl") or 0 for t in today_cl)
    realised_total = sum(t.get("pnl") or 0 for t in closed)
    unrealised     = sum(p.get("unrealized_pl", 0) for p in live.values())

    winners = [t for t in closed if (t.get("pnl") or 0) > 0]
    losers  = [t for t in closed if (t.get("pnl") or 0) <= 0]
    win_rate = len(winners) / len(closed) * 100 if closed else 0
    avg_win  = sum(t["pnl"] for t in winners) / len(winners) if winners else 0
    avg_loss = sum(t["pnl"] for t in losers)  / len(losers)  if losers  else 0

    lines = [
        "P&L SUMMARY",
        divider(),
        f"  Today realised       {pnl_str(realised_today):>12}",
        f"  All-time realised    {pnl_str(realised_total):>12}",
        f"  Open unrealised      {pnl_str(unrealised):>12}",
        f"  Net (realised+open)  {pnl_str(realised_total + unrealised):>12}",
        "",
        f"  Closed trades        {len(closed):>12}",
        f"  Win rate             {win_rate:>11.1f}%",
        f"  Avg winner           {pnl_str(avg_win) if winners else '—':>12}",
        f"  Avg loser            {pnl_str(avg_loss) if losers else '—':>12}",
    ]
    if winners and losers:
        rr = abs(avg_win / avg_loss) if avg_loss else 0
        lines.append(f"  Reward/risk ratio    {rr:>11.2f}×")

    return "\n".join(lines)


def section_skipped(skipped: List[Dict], today_only: bool = True) -> str:
    items = skipped
    if today_only:
        items = [s for s in skipped if s.get("date") == str(date.today())]

    title = f"SKIPPED TODAY — ≥4★ SETUPS NOT TRADED ({len(items)})"
    if not items:
        return title + "\n" + divider() + "\n  None."

    rows = [title, divider()]
    for s in items:
        detail_str = ""
        if s.get("detail"):
            d = s["detail"]
            parts = []
            if "orb_range" in d and "atr" in d:
                parts.append(f"ORB ${d['orb_range']:.2f} vs ATR ${d['atr']:.2f}")
            if "cost_estimate" in d and "available" in d:
                parts.append(f"cost ${d['cost_estimate']:,.0f} vs avail ${d['available']:,.0f}")
            detail_str = f"  [{', '.join(parts)}]" if parts else ""
        rows.append(f"  {s['symbol']:<7} {stars(s['stars'])}  {s['reason']}{detail_str}")

    return "\n".join(rows)


# ── Main ───────────────────────────────────────────────────────────────────────

def build_report(trades: List[Dict], skipped: List[Dict],
                 live: Dict, account: Dict, now_et: datetime) -> str:
    border = "═" * 64
    lines = [
        border,
        f"  TRADING BOT — DAILY REPORT",
        f"  {now_et.strftime('%A, %B %d %Y   %H:%M %Z')}",
        border,
        "",
        section_account(account),
        "",
        section_open(trades, live),
        "",
        section_closed(trades, today_only=True),
        "",
        section_summary(trades, live),
        "",
        section_skipped(skipped, today_only=True),
        "",
        border,
    ]
    return "\n".join(lines)


def run() -> None:
    now_et  = datetime.now(ET)
    trades, skipped = load_trades()
    live    = get_live_positions()
    account = get_account()

    report  = build_report(trades, skipped, live, account, now_et)

    print(report)

    # Save to archive
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    report_path = os.path.join(ARCHIVE_DIR, f"report_{date.today()}.txt")
    with open(report_path, "w") as f:
        f.write(report)
    print(f"\nReport saved → {report_path}")


if __name__ == "__main__":
    run()
