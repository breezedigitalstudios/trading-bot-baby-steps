"""
Email Report — sends EOD summary to REPORT_EMAIL after each pipeline run.
Requires GMAIL_USER and GMAIL_APP_PASSWORD env vars.
"""

import os
import json
import smtplib
import numpy as np
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import date, datetime, timezone, timedelta
from typing import Dict, List, Optional
import pytz
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

API_KEY            = os.getenv("ALPACA_API_KEY")
SECRET_KEY         = os.getenv("ALPACA_SECRET_KEY")
GMAIL_USER         = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
TO_EMAIL           = os.getenv("REPORT_EMAIL", GMAIL_USER)

if not API_KEY or not SECRET_KEY:
    raise RuntimeError("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env")

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client    = StockHistoricalDataClient(API_KEY, SECRET_KEY)

ET           = pytz.timezone("America/New_York")
TRADES_PATH  = os.path.join(os.path.dirname(__file__), "trades.json")
SCORES_PATH  = os.path.join(os.path.dirname(__file__), "setup_scores.json")
FUNNEL_PATH  = os.path.join(os.path.dirname(__file__), "funnel.json")


# ── Loaders ────────────────────────────────────────────────────────────────────

def load_trades():
    if not os.path.exists(TRADES_PATH):
        return [], []
    with open(TRADES_PATH) as f:
        p = json.load(f)
    return p.get("trades", []), p.get("skipped", [])


def load_setups() -> List[Dict]:
    if not os.path.exists(SCORES_PATH):
        return []
    with open(SCORES_PATH) as f:
        return json.load(f).get("high_quality", [])


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


def get_live_positions() -> Dict:
    try:
        return {
            p.symbol: {
                "current_price":   float(p.current_price),
                "unrealized_pl":   float(p.unrealized_pl),
                "avg_entry_price": float(p.avg_entry_price),
                "qty":             int(float(p.qty)),
            }
            for p in trading_client.get_all_positions()
        }
    except Exception as e:
        print(f"Warning: could not fetch positions: {e}")
        return {}


def get_last_close_prices(symbols: List[str]) -> Dict[str, float]:
    if not symbols:
        return {}
    prices = {}
    try:
        req  = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame.Day,
            start=datetime.now(timezone.utc) - timedelta(days=7),
        )
        bars = data_client.get_stock_bars(req).df
        if bars.empty:
            return {}
        for sym in symbols:
            try:
                lvl      = bars.index.get_level_values(0)
                sym_bars = bars.loc[sym].sort_index() if sym in lvl else bars.sort_index()
                if not sym_bars.empty:
                    prices[sym] = float(sym_bars["close"].iloc[-1])
            except Exception:
                pass
    except Exception as e:
        print(f"Warning: could not fetch setup prices: {e}")
    return prices


# ── HTML helpers ───────────────────────────────────────────────────────────────

NAVY  = "#1a2744"
GREEN = "#16a34a"
RED   = "#dc2626"
GRAY  = "#6b7280"

def stars_html(n: int) -> str:
    return f'<span style="color:#f59e0b;letter-spacing:1px">{"★" * n}{"☆" * (5 - n)}</span>'

def pnl_html(val: Optional[float]) -> str:
    if val is None:
        return '<span style="color:#9ca3af">—</span>'
    color = GREEN if val >= 0 else RED
    return f'<span style="color:{color};font-weight:600">${val:+,.2f}</span>'

def section(title: str, body: str) -> str:
    return f"""
    <div style="margin:0 0 24px 0">
      <div style="background:{NAVY};color:white;padding:10px 20px;font-size:13px;
                  font-weight:700;letter-spacing:1px;text-transform:uppercase">{title}</div>
      <div style="padding:0 4px">{body}</div>
    </div>"""

def table(headers: List[str], rows: List[List[str]], col_aligns: Optional[List[str]] = None) -> str:
    th_style = (f"padding:8px 12px;text-align:left;font-size:12px;font-weight:600;"
                f"color:{GRAY};border-bottom:1px solid #e5e7eb;white-space:nowrap")
    aligns = col_aligns or ["left"] * len(headers)

    head_cells = "".join(
        f'<th style="{th_style};text-align:{aligns[i]}">{h}</th>'
        for i, h in enumerate(headers)
    )
    body_rows = ""
    for ri, row in enumerate(rows):
        bg = "white" if ri % 2 == 0 else "#f9fafb"
        cells = "".join(
            f'<td style="padding:8px 12px;font-size:13px;text-align:{aligns[ci]};'
            f'border-bottom:1px solid #f3f4f6">{cell}</td>'
            for ci, cell in enumerate(row)
        )
        body_rows += f'<tr style="background:{bg}">{cells}</tr>'

    return (f'<table style="width:100%;border-collapse:collapse;font-family:Arial,sans-serif">'
            f'<thead><tr>{head_cells}</tr></thead>'
            f'<tbody>{body_rows}</tbody></table>')


def kv_table(pairs: List[tuple]) -> str:
    rows = ""
    for i, (label, value) in enumerate(pairs):
        bg = "white" if i % 2 == 0 else "#f9fafb"
        rows += (f'<tr style="background:{bg}">'
                 f'<td style="padding:8px 20px;font-size:13px;color:{GRAY};width:55%">{label}</td>'
                 f'<td style="padding:8px 20px;font-size:13px;font-weight:600;text-align:right">{value}</td>'
                 f'</tr>')
    return f'<table style="width:100%;border-collapse:collapse">{rows}</table>'


# ── Report sections ────────────────────────────────────────────────────────────

def section_account(account: Dict) -> str:
    if not account:
        return ""
    pairs = [
        ("Portfolio value",  f'${account["portfolio_value"]:,.2f}'),
        ("Cash",             f'${account["cash"]:,.2f}'),
        ("Buying power",     f'${account["buying_power"]:,.2f}'),
    ]
    return section("Account", kv_table(pairs))


def section_pnl(trades: List[Dict], live: Dict) -> str:
    today    = str(date.today())
    closed   = [t for t in trades if t.get("status") == "closed"]
    partial  = [t for t in trades if t.get("status") in ("partial_exit", "sma_exit_pending")]
    today_cl = [t for t in closed  if t.get("exit_date") == today]
    today_p1 = [t for t in partial if t.get("phase1_date") == today]

    # Realised = fully-closed trade pnl + locked-in phase1 partial profits
    realised_today = (sum(t.get("pnl")        or 0 for t in today_cl) +
                      sum(t.get("phase1_pnl") or 0 for t in today_p1))
    realised_total = (sum(t.get("pnl")        or 0 for t in closed) +
                      sum(t.get("phase1_pnl") or 0 for t in partial))
    unrealised     = sum(p.get("unrealized_pl", 0) for p in live.values())
    net            = realised_total + unrealised

    # Win-rate stats on fully closed trades only
    winners  = [t for t in closed if (t.get("pnl") or 0) > 0]
    losers   = [t for t in closed if (t.get("pnl") or 0) <= 0]
    win_rate = len(winners) / len(closed) * 100 if closed else 0
    avg_win  = sum(t["pnl"] for t in winners) / len(winners) if winners else 0
    avg_loss = sum(t["pnl"] for t in losers)  / len(losers)  if losers  else 0
    rr       = abs(avg_win / avg_loss) if avg_loss and avg_win else 0

    pairs = [
        ("Today realised",         pnl_html(realised_today)),
        ("All-time realised",      pnl_html(realised_total)),
        ("Open unrealised",        pnl_html(unrealised)),
        ("Net (realised + open)",  pnl_html(net)),
        ("",                       ""),
        ("Closed trades",          str(len(closed))),
        ("Win rate",               f"{win_rate:.1f}%"),
        ("Avg winner",             pnl_html(avg_win) if winners else "—"),
        ("Avg loser",              pnl_html(avg_loss) if losers  else "—"),
        ("Reward / risk",          f"{rr:.2f}×" if rr else "—"),
    ]
    return section("P&L Summary", kv_table(pairs))


def section_open(trades: List[Dict], live: Dict) -> str:
    open_trades = [t for t in trades if t.get("status") in ("open", "partial_exit", "sma_exit_pending")]
    if not open_trades:
        return section("Open Positions",
                        '<p style="padding:12px 20px;color:#9ca3af;font-size:13px;margin:0">None.</p>')

    rows = []
    for t in open_trades:
        sym    = t["symbol"]
        entry  = t.get("fill_price") or t["orb_high"]
        pos    = live.get(sym, {})
        curr   = pos.get("current_price", entry)
        unrlzd = pos.get("unrealized_pl")
        shares = t.get("shares_remaining", t["shares"])
        phase  = "partial" if t["status"] == "partial_exit" else ("closing" if t["status"] == "sma_exit_pending" else "trailing")
        fill_date = t.get("fill_date", str(date.today()))
        days   = int(np.busday_count(fill_date, str(date.today())))
        rows.append([
            f"<strong>{sym}</strong>",
            stars_html(t.get("stars", 0)),
            f"${entry:.2f}",
            f"${curr:.2f}",
            pnl_html(unrlzd),
            str(shares),
            str(days),
            phase,
        ])
    cols   = ["Symbol", "Stars", "Entry", "Current", "Unrlzd P&L", "Shares", "Days", "Phase"]
    aligns = ["left", "left", "right", "right", "right", "right", "right", "left"]
    return section("Open Positions", table(cols, rows, aligns))


def section_closed_today(trades: List[Dict]) -> str:
    today = str(date.today())

    # Fully closed trades exited today
    fully_closed = [t for t in trades
                    if t.get("status") == "closed" and t.get("exit_date") == today]
    # Partial exits that locked in profit today (phase1)
    partial_today = [t for t in trades
                     if t.get("status") == "partial_exit" and t.get("phase1_date") == today]

    if not fully_closed and not partial_today:
        return section("Closed Today",
                        '<p style="padding:12px 20px;color:#9ca3af;font-size:13px;margin:0">None.</p>')

    rows = []
    for t in sorted(fully_closed, key=lambda x: x.get("exit_date", ""), reverse=True):
        entry  = t.get("fill_price") or t.get("orb_high", 0)
        exit_p = t.get("exit_price", 0) or 0
        rows.append([
            f"<strong>{t['symbol']}</strong>",
            stars_html(t.get("stars", 0)),
            f"${entry:.2f}",
            f"${exit_p:.2f}",
            pnl_html(t.get("pnl")),
            t.get("exit_reason", "?"),
        ])
    for t in sorted(partial_today, key=lambda x: x.get("phase1_date", ""), reverse=True):
        entry    = t.get("fill_price") or t.get("orb_high", 0)
        shares   = t["shares"] - t.get("shares_remaining", t["shares"])
        rows.append([
            f"<strong>{t['symbol']}</strong>",
            stars_html(t.get("stars", 0)),
            f"${entry:.2f}",
            "~mkt",
            pnl_html(t.get("phase1_pnl")),
            f"phase1 partial ({shares} shares)",
        ])
    cols   = ["Symbol", "Stars", "Entry", "Exit", "P&L", "Reason"]
    aligns = ["left", "left", "right", "right", "right", "left"]
    return section("Closed Today", table(cols, rows, aligns))


_CRITERIA_LABELS = {
    "ma_aligned":       "Trend alignment",
    "higher_lows":      "Higher lows",
    "range_tightening": "Range tightening",
    "narrow_candle":    "Narrow candle",
    "volume_dryup":     "Volume dry-up",
}

def section_setups(setups: List[Dict], prices: Dict[str, float]) -> str:
    if not setups:
        return section("Tomorrow's Setups",
                        '<p style="padding:12px 20px;color:#9ca3af;font-size:13px;margin:0">No high-quality setups.</p>')

    rows = []
    for s in setups:
        sym   = s["symbol"]
        price = prices.get(sym)
        price_str = f"${price:.2f}" if price else "—"
        breakdown = s.get("breakdown", {})
        criteria  = ", ".join(label for k, label in _CRITERIA_LABELS.items() if breakdown.get(k))
        rows.append([
            f"<strong>{sym}</strong>",
            stars_html(s["stars"]),
            price_str,
            f'<span style="color:{GRAY};font-size:12px">{criteria}</span>',
        ])
    cols   = ["Symbol", "Stars", "Last Close", "Criteria met"]
    aligns = ["left", "left", "right", "left"]
    return section("Tomorrow's High-Quality Setups", table(cols, rows, aligns))


# ── Funnel section ────────────────────────────────────────────────────────────

_FUNNEL_REASON_LABELS: Dict[str, str] = {
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


def _fmt_syms(syms: List[dict], max_show: int = 6) -> str:
    if not syms:
        return f'<span style="color:{GRAY}">—</span>'
    names = [s["symbol"] for s in syms]
    shown = "  ".join(f"<strong>{n}</strong>" for n in names[:max_show])
    if len(names) > max_show:
        shown += f' <span style="color:{GRAY}">+{len(names) - max_show} more</span>'
    return shown


def section_funnel(today: str) -> str:
    if not os.path.exists(FUNNEL_PATH):
        return ""
    with open(FUNNEL_PATH) as f:
        funnel_data = json.load(f)
    if today not in funnel_data:
        return ""

    e = funnel_data[today]
    regime        = e.get("regime", "?")
    regime_reason = e.get("regime_reason", "")
    regime_color  = GREEN if regime == "TRADE" else RED

    td_main = (f"padding:8px 14px;font-size:13px;font-weight:600;"
               f"border-bottom:1px solid #f3f4f6")
    td_sub  = (f"padding:5px 14px 5px 30px;font-size:12px;color:#374151;"
               f"border-bottom:1px solid #f3f4f6")
    td_num  = (f"padding:8px 14px;font-size:13px;font-weight:700;text-align:right;"
               f"white-space:nowrap;border-bottom:1px solid #f3f4f6")
    td_num_sub = (f"padding:5px 14px;font-size:12px;text-align:right;"
                  f"white-space:nowrap;border-bottom:1px solid #f3f4f6")
    td_sym  = (f"padding:8px 14px;font-size:12px;border-bottom:1px solid #f3f4f6")
    td_sym_sub = (f"padding:5px 14px;font-size:12px;color:#6b7280;"
                  f"border-bottom:1px solid #f3f4f6")

    def main_row(label, count, sym_html, bg="white"):
        return (f'<tr style="background:{bg}">'
                f'<td style="{td_main}">{label}</td>'
                f'<td style="{td_num}">{count}</td>'
                f'<td style="{td_sym}">{sym_html}</td>'
                f'</tr>')

    def sub_row(label, count, sym_html):
        return (f'<tr style="background:#f9fafb">'
                f'<td style="{td_sub}">↳ {label}</td>'
                f'<td style="{td_num_sub}">{count}</td>'
                f'<td style="{td_sym_sub}">{sym_html}</td>'
                f'</tr>')

    regime_badge = (f'<span style="background:{regime_color};color:white;font-size:11px;'
                    f'padding:2px 7px;border-radius:3px;font-weight:700">{regime}</span>')
    if regime_reason:
        regime_badge += f' <span style="color:{GRAY};font-size:11px">[{regime_reason}]</span>'

    rows = ""

    # Regime header row
    rows += (f'<tr style="background:#f0f4ff">'
             f'<td colspan="3" style="padding:8px 14px;font-size:12px;'
             f'border-bottom:1px solid #e5e7eb">Market regime: {regime_badge}</td>'
             f'</tr>')

    # Scan pass
    rows += main_row(
        "Scan pass (watchlist)",
        e["scan_pass"]["count"],
        f'<span style="color:{GRAY};font-size:11px">all candidates</span>',
        bg="white",
    )

    # Setups
    rows += sub_row("4★ setups", e["setups_4star"]["count"],
                    _fmt_syms(e["setups_4star"]["symbols"]))
    rows += sub_row("5★ setups", e["setups_5star"]["count"],
                    _fmt_syms(e["setups_5star"]["symbols"]))

    # Skipped
    skipped = e.get("skipped", {})
    if skipped:
        rows += (f'<tr style="background:#fff7ed">'
                 f'<td colspan="3" style="padding:6px 14px;font-size:11px;font-weight:700;'
                 f'color:#92400e;letter-spacing:0.5px;border-bottom:1px solid #f3f4f6">'
                 f'SKIPPED TODAY</td></tr>')
        for key, val in skipped.items():
            label = _FUNNEL_REASON_LABELS.get(key, key)
            rows += sub_row(label, val["count"], _fmt_syms(val["symbols"]))

    # Orders
    fill_color = GREEN if e["orders_filled"]["count"] > 0 else GRAY
    rows += main_row(
        "Orders placed",
        e["orders_placed"]["count"],
        _fmt_syms(e["orders_placed"]["symbols"]),
        bg="white",
    )
    rows += (f'<tr style="background:white">'
             f'<td style="{td_main};padding-left:30px">↳ Filled</td>'
             f'<td style="{td_num};color:{fill_color}">{e["orders_filled"]["count"]}</td>'
             f'<td style="{td_sym}">{_fmt_syms(e["orders_filled"]["symbols"])}</td>'
             f'</tr>')

    header_row = (f'<tr>'
                  f'<th style="padding:8px 14px;text-align:left;font-size:12px;'
                  f'font-weight:600;color:{GRAY};border-bottom:2px solid #e5e7eb">Stage</th>'
                  f'<th style="padding:8px 14px;text-align:right;font-size:12px;'
                  f'font-weight:600;color:{GRAY};border-bottom:2px solid #e5e7eb">Count</th>'
                  f'<th style="padding:8px 14px;text-align:left;font-size:12px;'
                  f'font-weight:600;color:{GRAY};border-bottom:2px solid #e5e7eb">Symbols</th>'
                  f'</tr>')

    body = (f'<table style="width:100%;border-collapse:collapse;font-family:Arial,sans-serif">'
            f'<thead>{header_row}</thead>'
            f'<tbody>{rows}</tbody>'
            f'</table>')

    return section(f"Today's Pipeline Funnel", body)


# ── Build full HTML email ──────────────────────────────────────────────────────

def build_email_html(trades, setups, live, account, now_et) -> str:
    setup_prices = get_last_close_prices([s["symbol"] for s in setups])

    today_str = now_et.strftime("%Y-%m-%d")
    body_sections = (
        section_account(account)
        + section_pnl(trades, live)
        + section_funnel(today_str)
        + section_open(trades, live)
        + section_closed_today(trades)
        + section_setups(setups, setup_prices)
    )

    return f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:20px 0;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif">
  <div style="max-width:680px;margin:0 auto;background:white;border-radius:8px;
              overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.08)">

    <div style="background:{NAVY};padding:28px 32px">
      <div style="color:white;font-size:20px;font-weight:700;margin:0 0 4px">
        Trading Bot — EOD Report
      </div>
      <div style="color:#8fa3c8;font-size:13px">
        {now_et.strftime('%A, %B %d %Y  ·  %H:%M %Z')}
      </div>
      <div style="margin-top:12px">
        <a href="https://breezedigitalstudios.github.io/trading-bot-baby-steps/dashboard_v2.html"
           style="display:inline-block;background:white;color:{NAVY};font-size:12px;font-weight:700;
                  padding:6px 16px;border-radius:4px;text-decoration:none">
          View Dashboard →
        </a>
      </div>
    </div>

    <div style="padding:24px 16px 8px">
      {body_sections}
    </div>

    <div style="padding:16px 32px;background:#f9fafb;border-top:1px solid #e5e7eb;
                font-size:11px;color:#9ca3af;text-align:center">
      Qullamaggie Breakout Bot · paper trading · auto-generated
    </div>

  </div>
</body>
</html>"""


# ── Send ───────────────────────────────────────────────────────────────────────

def build_subject(trades: List[Dict], live: Dict) -> str:
    closed      = [t for t in trades if t.get("status") == "closed"]
    today_cl    = [t for t in closed if t.get("exit_date") == str(date.today())]
    realised_td = sum(t.get("pnl") or 0 for t in today_cl)
    unrealised  = sum(p.get("unrealized_pl", 0) for p in live.values())
    net         = realised_td + unrealised
    sign        = "+" if net >= 0 else ""
    return f"Trading Bot · {date.today()}  |  Today {sign}${net:,.0f}"


def send(html: str, subject: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = TO_EMAIL
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.sendmail(GMAIL_USER, TO_EMAIL, msg.as_string())
    print(f"Email sent to {TO_EMAIL}")


def run() -> None:
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        raise RuntimeError("Set GMAIL_USER and GMAIL_APP_PASSWORD in .env or GitHub secrets")

    now_et  = datetime.now(ET)
    trades, _ = load_trades()
    setups    = load_setups()
    live      = get_live_positions()
    account   = get_account()

    html    = build_email_html(trades, setups, live, account, now_et)
    subject = build_subject(trades, live)
    send(html, subject)


if __name__ == "__main__":
    run()
