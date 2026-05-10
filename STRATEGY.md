# Trading Bot Strategy

Based on Kristjan Kullamägi (Qullamaggie) — Breakout Setup

---

## Core Philosophy

Stocks move in stair-step patterns. The goal is to identify the next step higher just before it forms. Win rate is intentionally low (~25-30%), but small losses combined with occasional large winners (10-20× risk) generate the returns. Patience and discipline over activity.

---

## 1. Market Regime Filter

**Check daily before allowing any new entries. Proxy: NASDAQ.**

| Condition | Action |
|---|---|
| 10 SMA sloping up, 20 SMA sloping up, 10 SMA above 20 SMA | TRADE — new longs allowed |
| 10 SMA sloping down, 20 SMA sloping down, 10 SMA below 20 SMA | CASH — no new entries |
| Choppy / mixed signals | CASH — no new entries |

When regime turns bad with open positions: **let existing positions run with their stops, block only new entries.**

---

## 2. Universe Scan

Run end-of-day. Three momentum scans, each looking for biggest gainers:

| Scan | Formula | Lookback |
|---|---|---|
| 1-Month | `C / Min(L, 22)` | 22 trading days |
| 3-Month | `C / Min(L, 67)` | 67 trading days |
| 6-Month | `C / Min(L, 126)` | 126 trading days |

**Rank:** top 7% (≥ 93rd percentile) of the broad market for each scan.

**Filters applied to all scans:**
- ADR (Average Daily Range) ≥ 4% — *"Low ADR = shit, high ADR = gold"*
- Stock price ≥ $20 (filters out penny stocks and illiquid names)
- Dollar volume ≥ $20M
- Cap results at ~50 candidates per scan

Deduplicate across the three scans → unified watchlist.

---

## 3. Setup Detection & Scoring

Score each watchlist candidate 1–5 stars. **Only trade setups scoring 4 stars or above.**

| Criterion | What to look for |
|---|---|
| Prior big move | Stock is in top 1-2% of performers — a clear momentum leader |
| Pullback / consolidation | Orderly pullback or sideways base after the big move |
| MA support | Price finding support on 10 SMA, 20 SMA, or 50 SMA |
| Higher lows | Each swing low is higher than the previous during consolidation |
| Tightening range | Daily candles getting progressively narrower (ATR contracting) |
| Narrow-range candle | Day before breakout has a notably small range (ideal, not required) |
| Volume on breakout | Breakout day volume significantly above average |
| Trend angle | Stock moving at ≥ 45° angle on daily chart — steeper is better |
| Sector strength | Leading sector adds confidence |
| Relative strength | Holds up well vs. market during the pullback |

**Indicators used (only these):**
- Daily chart: 10 SMA, 20 SMA, 50 SMA
- 60-minute chart: 10 EMA, 20 EMA, 65 EMA
- ADR (Average Daily Range %)
- ATR (Average True Range, 14-day, absolute value)

---

## 4. Entry Rules

**Trigger:** breakout above the Opening Range High on the first 60-minute candle (9:30–10:30 AM).

**Pre-entry validation (all must pass):**
1. `ORB high − ORB low ≤ ATR` — if the range exceeds ATR, skip the trade
2. No earnings announcement within the next 2–3 trading days
3. Market regime is TRADE (see Section 1)
4. Fewer than 4 open positions currently held
5. Fewer than 2 new positions opened today

**Entry:** place buy-stop order at ORB high, stop-loss at ORB low.

---

## 5. Position Sizing

```
ATR          = 14-day ATR on the daily chart
ORB high     = high of the 9:30–10:30 AM candle  (entry price)
ORB low      = low of the 9:30–10:30 AM candle   (stop price)
Risk/share   = ORB high − ORB low

Shares       = (Account value × 10%) / Risk/share
Shares       = min(Shares, Account value × 25% / ORB high)
```

**Hard limits:**
- Risk per trade: **10% of account** (to be reduced to 1-2% for live trading)
- Max position size: **25% of account value** per stock
- Max concurrent positions: **4**
- Cash buffer: always keep **25% of account in cash** — never deploy more than 75%
- Max new positions per day: **2**

---

## 6. Stop Loss & Exit Rules

### Initial Stop
- **Stop price = ORB low** (low of the first 60-min candle)
- This is a hard stop — no exceptions

### Phase 1 — Partial Exit (Day 3)
- After **3 full trading days** of follow-through (weekends excluded), if the position is profitable:
  - Sell **1/3 of the position**
  - Move stop on remaining shares to **breakeven (entry price)**

### Phase 2 — Trail the Runner
- Trail remaining shares using the **10-day SMA** on the daily chart
- Exit the remainder on the **first daily close below the 10 SMA**
- Intraday breaks of the 10 SMA that recover by close: **ignore**
- For slower-moving stocks, the 20 SMA can be substituted

---

## 7. Bot Architecture

| Module | Schedule | Function |
|---|---|---|
| `scanner.py` | EOD (after market close) | Runs 3 momentum scans, applies ADR + dollar volume filters, outputs watchlist |
| `setup_detector.py` | EOD | Scores each watchlist candidate 1-5 stars, filters to ≥4 |
| `regime_filter.py` | EOD + pre-market | Checks NASDAQ 10/20 SMA — returns TRADE or CASH |
| `entry_executor.py` | 9:30–10:30 AM | Monitors 60-min ORB breakouts, validates ATR stop, places bracket orders |
| `position_manager.py` | Intraday + EOD | Day-3 partial exit, breakeven stop move, 10 SMA trailing exit |
| `logger.py` | On every trade event | Records entry, exit, setup score, P&L per trade |

---

## 8. Hard Rules (Non-Negotiable)

- Stop ≤ ATR from entry — if wider, skip the trade
- Never more than 25% of account in any single stock
- Never more than 75% of account deployed at once
- Never more than 4 open positions
- Never more than 2 new entries per day
- Exit on the first close below 10 SMA — no second chances
- No new longs when market regime is CASH
- No entries within 2-3 days of earnings

---

## 9. Key Reminders

- Win rate will be ~25-30% — **losses are expected and normal**
- Pattern quality is partially judgmental; strict thresholds are an approximation
- The exit rules are where most profits come from — respect them
- In bad markets, even perfect setups fail — sit in cash and wait
- Risk per trade will be reduced to 1-2% before deploying live capital
