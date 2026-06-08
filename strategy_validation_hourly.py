"""
Qullamaggie Breakout — Hourly Intraday Backtest
- yfinance 1h interval (~730 days max available)
- Proper 60-min ORB entry simulation
- Intraday stop execution (not just EOD)
- 150+ ticker universe
"""
import warnings; warnings.filterwarnings('ignore')
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
import json, math

# ── CONFIG ──────────────────────────────────────────────────────────────────
END       = datetime.today().strftime("%Y-%m-%d")
START     = (datetime.today() - timedelta(days=728)).strftime("%Y-%m-%d")
INIT_CAP  = 100_000.0
COST_SIDE = 0.0025      # 0.25% per trade leg (slippage + spread + commission)
MAX_POS   = 0.25        # max 25% per position → max 4 concurrent
MIN_PRICE = 20.0
MIN_DVOL  = 20e6
ATR_N     = 14
PARTIAL_FRAC = 0.40
PARTIAL_DAYS = 3        # trading days
RF        = 0.04

# ── EXPANDED UNIVERSE ───────────────────────────────────────────────────────
UNIVERSE = list(dict.fromkeys([   # dict.fromkeys preserves order and deduplicates
    # Mega-cap tech/growth
    "NVDA","AMD","MSFT","AAPL","META","AMZN","GOOGL","TSLA","AVGO","QCOM",
    # Semiconductors & equipment
    "SMCI","LRCX","KLAC","AMAT","MRVL","MCHP","ARM","ONTO","ENTG","MPWR",
    "WOLF","SWKS","CRUS","AMBA","ACMR","SLAB","OLED","COHU","AAOI","AEHR",
    # AI / Cloud / Cybersecurity
    "PLTR","NET","DDOG","SNOW","CRWD","PANW","ZS","FTNT","HUBS","GTLB",
    "CFLT","SMAR","DOCN","MDB","FIVN","CWAN","ASAN","APP","BRZE","AXON",
    "ANET","PSTG","NTAP","VERX","RDDT","HOOD",
    # Fintech & crypto-adjacent
    "COIN","AFRM","SOFI","UPST","NU","PYPL","MARA","RIOT","CLSK","CIFR",
    # Healthcare / Biotech
    "NTRA","ALNY","INSP","IRTC","PCVX","INSM","TMDX","VKTX","PRCT",
    "SWAV","NVCR","RGEN","HIMS","STVN","LNTH","RXRX","ARWR","MRNA","BNTX",
    "ACAD","SUPN","AXSM","APGE","IMNM","VCEL","ITOS","KROS","KRYS","SAGE",
    "IONS","SRPT","BMRN","RARE","FOLD","DNLI","PTGX","TPST","IMVT","ACMR",
    # Consumer / Retail / Apparel
    "LULU","DECK","ONON","CROX","ELF","ULTA","CELH","MNST","CAVA","WING",
    "DUOL","BIRK","TPR","SKX","MODG","DNUT","BROS",
    # Industrial / Infrastructure / Energy
    "PODD","GNRC","ENPH","VRT","ETN","PWR","MTDR","NOG","CIVI","FANG",
    "DVN","MPC","PSX","SLB","HAL","OXY","TRGP","AM",
    # Growth internet / platform
    "MELI","SHOP","RBLX","PINS","TTD","MGNI","SE","NFLX","UBER","ABNB",
    "DASH","LYFT","SPOT","SNAP","TWLO","OKTA","TEAM","WDAY","NOW","ADSK",
    # Space / Autonomy / Disruptive
    "ASTS","LUNR","RKLB","ACHR","JOBY","IONQ","QUBT","RGTI","SOUN","BBAI",
    "BLDE","RDW","MNTS","SPCE",
    # Med devices / diagnostics
    "DXCM","ISRG","RMD","ALGN","IDXX","FICO","GRMN","ODFL","WST","PODD",
    # Additional large-cap momentum
    "LLY","REGN","VRTX","GILD","BIIB","NVO","ABBV","TMO","DHR","ABT",
    "GLOB","IBKR","LPLA","MKTX","HUM","CI","MOH",
]))

BENCHMARKS = {"SPY": "S&P 500", "QQQ": "Nasdaq 100", "MTUM": "Momentum ETF"}

# ── DATA DOWNLOAD ────────────────────────────────────────────────────────────
def download_hourly(tickers, start, end):
    """Download 1h OHLCV. Returns (Price, Ticker) MultiIndex DataFrame."""
    print(f"  Downloading 1h data for {len(tickers)} tickers…", flush=True)
    raw = yf.download(tickers, start=start, end=end,
                      interval="1h", auto_adjust=True,
                      progress=False)
    if raw.empty:
        raise RuntimeError("No hourly data returned from yfinance")
    print(f"  Raw shape: {raw.shape}", flush=True)
    return raw

def extract_field(raw, field):
    """Extract one price field from (Price, Ticker) MultiIndex columns."""
    if isinstance(raw.columns, pd.MultiIndex):
        if field in raw.columns.get_level_values(0):
            df = raw[field]
            return df if isinstance(df, pd.DataFrame) else df.to_frame()
    return pd.DataFrame()

def download_daily_field(tickers, field, start, end):
    """Separate daily download for regime + SMA calculations."""
    raw = yf.download(tickers, start=start, end=end,
                      interval="1d", auto_adjust=True, progress=False)
    if raw.empty: return pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex):
        if field in raw.columns.get_level_values(0):
            df = raw[field]
            return df if isinstance(df, pd.DataFrame) else df.to_frame()
    return pd.DataFrame()

# ── TIMEZONE & DATE HELPERS ──────────────────────────────────────────────────
def to_et(idx):
    """Convert index to America/New_York."""
    if idx.tz is None:
        return idx.tz_localize('UTC').tz_convert('America/New_York')
    return idx.tz_convert('America/New_York')

def market_date(ts):
    """Date part of a NY-localized timestamp."""
    return ts.date()

# ── DAILY AGGREGATES FROM HOURLY ─────────────────────────────────────────────
def hourly_to_daily(h_close, h_high, h_low, h_vol):
    """Resample hourly bars to daily OHLCV."""
    idx_et = to_et(h_close.index)
    dates_ser = pd.Series(idx_et.date, index=h_close.index)

    d_close = h_close.groupby(dates_ser).last()
    d_high  = h_high.groupby(dates_ser).max()
    d_low   = h_low.groupby(dates_ser).min()
    d_vol   = h_vol.groupby(dates_ser).sum()
    d_close.index = pd.to_datetime(d_close.index)
    d_high.index  = pd.to_datetime(d_high.index)
    d_low.index   = pd.to_datetime(d_low.index)
    d_vol.index   = pd.to_datetime(d_vol.index)
    return d_close, d_high, d_low, d_vol

# ── INDICATORS ───────────────────────────────────────────────────────────────
def sma(df, n):
    return df.rolling(n, min_periods=n).mean()

def atr_daily(dh, dl, dc, n=14):
    tr = pd.concat([dh-dl, (dh-dc.shift()).abs(), (dl-dc.shift()).abs()], axis=1)
    if isinstance(tr.columns, pd.MultiIndex): tr = tr.droplevel(0, axis=1)
    return tr.groupby(tr.columns, axis=1).max() if False else \
           pd.DataFrame({c: pd.concat([dh[c]-dl[c], (dh[c]-dc[c].shift()).abs(),
                                        (dl[c]-dc[c].shift()).abs()], axis=1).max(axis=1)
                          for c in dh.columns}).ewm(span=n, adjust=False).mean()

# ── MARKET REGIME (daily QQQ) ────────────────────────────────────────────────
def make_regime(qqq_daily):
    s10 = qqq_daily.rolling(10).mean()
    s20 = qqq_daily.rolling(20).mean()
    return (s10 > s20) & (s10 > s10.shift(1)) & (s20 > s20.shift(1))

# ── SETUP FILTER (daily, checked before each trading day) ───────────────────
def daily_setup_flags(d_close, d_high, d_low, d_vol):
    """
    Returns boolean DataFrame (dates × tickers) — True if ticker qualifies for
    ORB entry watch on that day (checked using data UP TO prior close).
    """
    dvol = d_close * d_vol
    adr_ = ((d_high - d_low) / d_low.replace(0, np.nan) * 100).rolling(20).mean()
    s10  = sma(d_close, 10)
    s20  = sma(d_close, 20)
    s50  = sma(d_close, 50)

    # Momentum: close at least 10% above 22-day low → strong prior move
    mom22  = d_close / d_low.rolling(22).min()
    # Uptrend structure
    trend    = (s10 > s10.shift(3)) & (d_close > s20) & (s20 > s50)
    # Higher lows (last 5 days)
    high_lows = d_low > d_low.shift(1).rolling(5).min()
    # Quality filters
    liq    = (d_close >= MIN_PRICE) & (dvol >= MIN_DVOL)
    setup  = (adr_ >= 4) & (mom22 >= 1.10) & trend & high_lows & liq

    return setup.shift(1).fillna(False).astype(bool)  # use PRIOR day's data

# ── ORB DATA (per day, per ticker) ──────────────────────────────────────────
def build_orb_table(h_open, h_high, h_low, h_close, h_vol):
    """
    Returns dict: date -> {ticker: {orb_high, orb_low, orb_vol}}
    ORB = first hourly candle of each trading day (9:30 candle).
    """
    idx_et  = to_et(h_close.index)
    # Group by date × take first row
    date_col = pd.Series(idx_et.date, index=h_close.index)
    orb_high = h_high.groupby(date_col).first()
    orb_low  = h_low.groupby(date_col).first()
    orb_vol  = h_vol.groupby(date_col).first()
    orb_high.index = pd.to_datetime(orb_high.index)
    orb_low.index  = pd.to_datetime(orb_low.index)
    orb_vol.index  = pd.to_datetime(orb_vol.index)
    return orb_high, orb_low, orb_vol

# ── HOURLY VOLUME AVERAGE (for volume expansion check) ──────────────────────
def hourly_vol_avg(h_vol, window_days=20):
    """Rolling N-day average of hourly volumes (summed per day)."""
    idx_et  = to_et(h_vol.index)
    date_col = pd.Series(idx_et.date, index=h_vol.index)
    daily_vol = h_vol.groupby(date_col).sum()
    daily_vol.index = pd.to_datetime(daily_vol.index)
    avg = daily_vol.rolling(window_days).mean()
    # Map back to hourly: each hourly row gets its day's avg
    avg_hourly = avg.reindex(date_col.values).values
    return pd.DataFrame(avg_hourly, index=h_vol.index, columns=h_vol.columns)

# ── SIMULATION ENGINE ─────────────────────────────────────────────────────────
def simulate_hourly(h_open, h_high, h_low, h_close, h_vol,
                    d_close, d_high, d_low, regime_series,
                    setup_flags, orb_h, orb_l, orb_v):
    """
    Full intraday simulation.
    - Entry: first post-ORB hourly candle that closes above ORB high with vol expansion
    - Stop: ORB low (checked on every hourly low)
    - Phase-1: partial exit EOD of day 3 if profitable
    - Phase-2: trail 10-day SMA (daily close)
    Returns equity curve (daily) and trades DataFrame.
    """
    idx_et   = to_et(h_close.index)
    date_col = pd.Series(idx_et.date, index=h_close.index)
    trading_days = sorted(set(date_col.values))

    s10_daily = sma(d_close, 10)  # for trailing exit
    atr_d     = atr_daily(d_high, d_low, d_close, ATR_N)

    # Avg hourly vol for volume expansion check (day-level)
    d_vol_sum = h_vol.groupby(date_col.values).sum()
    d_vol_sum.index = pd.to_datetime(d_vol_sum.index)
    avg_dvol = d_vol_sum.rolling(20).mean()

    cash       = INIT_CAP
    positions  = {}   # tk -> {shares, ep, entry_date, stop, partial, entry_dt_i}
    trades     = []
    equity_daily = {}  # date -> portfolio value

    tickers = h_close.columns.tolist()

    for day_i, date in enumerate(trading_days):
        dt_key = pd.Timestamp(date)
        day_mask = date_col.values == date
        day_idx  = h_close.index[day_mask]

        if len(day_idx) < 2:
            equity_daily[date] = cash + sum(
                pos['shares'] * (d_close.loc[dt_key, tk]
                                 if (dt_key in d_close.index and tk in d_close.columns)
                                 else pos['ep'])
                for tk, pos in positions.items()
            )
            continue

        # ── Portfolio value at day start (mark-to-market on prior close) ──
        port_val = cash
        for tk, pos in positions.items():
            px = (d_close.loc[dt_key, tk]
                  if dt_key in d_close.index and tk in d_close.columns and
                  not pd.isna(d_close.loc[dt_key, tk] if dt_key in d_close.index else np.nan)
                  else pos['ep'])
            port_val += pos['sh'] * px

        regime_ok = (regime_series.loc[dt_key]
                     if dt_key in regime_series.index else False)
        avail_slots = max(0, 4 - len(positions))

        # ── ORB data for today ──
        orb_high_today = orb_h.loc[dt_key] if dt_key in orb_h.index else pd.Series(dtype=float)
        orb_low_today  = orb_l.loc[dt_key] if dt_key in orb_l.index else pd.Series(dtype=float)

        # ── Intraday loop: each hourly bar ──
        for bar_i, bar_ts in enumerate(day_idx):
            cl_bar  = h_close.loc[bar_ts]
            hi_bar  = h_high.loc[bar_ts]
            lo_bar  = h_low.loc[bar_ts]
            vol_bar = h_vol.loc[bar_ts]

            # ── Check exits for all open positions ──
            to_rm = []
            for tk, pos in positions.items():
                if tk not in h_close.columns: continue
                lo = lo_bar.get(tk, np.nan)
                cl = cl_bar.get(tk, np.nan)
                if pd.isna(lo) or pd.isna(cl): continue

                exit_px = None; reason = None

                # Intraday stop: low crosses stop
                if lo <= pos['stop']:
                    exit_px = max(pos['stop'] * (1 - COST_SIDE),
                                  lo * (1 - COST_SIDE))
                    reason  = "stop"

                if exit_px and reason:
                    pnl = pos['sh'] * (exit_px - pos['ep'])
                    cash += pos['sh'] * exit_px
                    trades.append(dict(ticker=tk, entry_dt=pos['entry_dt'],
                                       exit_dt=str(bar_ts), ep=pos['ep'],
                                       xp=exit_px, sh=pos['sh'],
                                       pnl=pnl, reason=reason,
                                       days_held=pos['days']))
                    to_rm.append(tk)
            for tk in to_rm: positions.pop(tk, None)

            # ── ORB breakout entries (skip first bar = ORB itself) ──
            if bar_i == 0 or not regime_ok or avail_slots <= 0:
                continue

            for tk in tickers:
                if tk in positions or tk not in cl_bar.index: continue
                cl  = cl_bar.get(tk, np.nan)
                lo  = lo_bar.get(tk, np.nan)
                vol = vol_bar.get(tk, np.nan)

                if pd.isna(cl) or pd.isna(vol): continue

                orb_hi = orb_high_today.get(tk, np.nan)
                orb_lo = orb_low_today.get(tk, np.nan)

                if pd.isna(orb_hi) or pd.isna(orb_lo): continue

                # Breakout: this bar's close > ORB high
                if cl <= orb_hi: continue

                # Volume expansion: today's partial vol > 60% of daily avg
                # (we only have partial day vol; use a scaled check)
                avg_v = (avg_dvol.loc[dt_key, tk]
                         if dt_key in avg_dvol.index and tk in avg_dvol.columns
                         else np.nan)
                if not pd.isna(avg_v) and avg_v > 0 and vol < avg_v * 0.30:
                    continue  # weak volume

                # Setup: check prior-day flags
                if dt_key not in setup_flags.index: continue
                if not setup_flags.loc[dt_key, tk] if tk in setup_flags.columns else False:
                    continue

                # Not extended: entry not more than 1.5×ATR above ORB low
                at = (atr_d.loc[dt_key, tk]
                      if dt_key in atr_d.index and tk in atr_d.columns else np.nan)
                if not pd.isna(at) and (cl - orb_lo) > 1.5 * at:
                    continue

                # Price / liquidity check (live on this bar)
                if cl < MIN_PRICE: continue
                # dvol check done via setup_flags already

                # Size
                pos_sz = min(MAX_POS * port_val, cash * 0.95)
                ep     = cl * (1 + COST_SIDE)
                sh     = int(pos_sz / ep)
                if sh < 1 or sh * ep > cash: continue

                cash -= sh * ep
                stop  = orb_lo * (1 - 0.002)  # just below ORB low
                positions[tk] = dict(sh=sh, ep=cl, entry_dt=str(bar_ts),
                                     entry_date=date, stop=stop,
                                     partial=False, days=0)
                avail_slots -= 1
                if avail_slots <= 0: break

        # ── EOD processing ──
        to_rm = []
        for tk, pos in positions.items():
            # Increment days held counter
            pos['days'] += 1

            if tk not in d_close.columns or dt_key not in d_close.index: continue
            cl_eod = d_close.loc[dt_key, tk]
            sm10   = (s10_daily.loc[dt_key, tk]
                      if dt_key in s10_daily.index and tk in s10_daily.columns else np.nan)
            if pd.isna(cl_eod): continue

            # Phase-1: partial exit after PARTIAL_DAYS trading days
            if not pos['partial'] and pos['days'] >= PARTIAL_DAYS and cl_eod > pos['ep']:
                n = int(pos['sh'] * PARTIAL_FRAC)
                if n > 0:
                    ep_out = cl_eod * (1 - COST_SIDE)
                    cash  += n * ep_out
                    pnl    = n * (ep_out - pos['ep'])
                    trades.append(dict(ticker=tk, entry_dt=pos['entry_dt'],
                                       exit_dt=str(dt_key), ep=pos['ep'],
                                       xp=ep_out, sh=n, pnl=pnl,
                                       reason='partial', days_held=pos['days']))
                    pos['sh']      -= n
                    pos['partial']  = True
                    pos['stop']     = pos['ep']  # breakeven stop

            # Phase-2: trail 10-day SMA exit
            if pos['partial'] and not pd.isna(sm10) and cl_eod < sm10:
                ep_out = cl_eod * (1 - COST_SIDE)
                pnl    = pos['sh'] * (ep_out - pos['ep'])
                cash  += pos['sh'] * ep_out
                trades.append(dict(ticker=tk, entry_dt=pos['entry_dt'],
                                   exit_dt=str(dt_key), ep=pos['ep'],
                                   xp=ep_out, sh=pos['sh'], pnl=pnl,
                                   reason='sma10_trail', days_held=pos['days']))
                to_rm.append(tk)

        for tk in to_rm: positions.pop(tk, None)

        # ── EOD portfolio value ──
        total = cash
        for tk, pos in positions.items():
            px = (d_close.loc[dt_key, tk]
                  if dt_key in d_close.index and tk in d_close.columns
                  and not pd.isna(d_close.loc[dt_key, tk] if dt_key in d_close.index else np.nan)
                  else pos['ep'])
            total += pos['sh'] * px
        equity_daily[date] = total

    eq = pd.Series({pd.Timestamp(k): v for k, v in equity_daily.items()}).sort_index()
    eq = eq.replace(0, np.nan).ffill().bfill()
    return eq, pd.DataFrame(trades)

# ── METRICS ──────────────────────────────────────────────────────────────────
def perf(eq, tdf, label):
    ret = eq.pct_change().dropna()
    ann = 252; yrs = len(eq) / ann
    if yrs <= 0: return {}
    cagr = (eq.iloc[-1]/eq.iloc[0])**(1/yrs) - 1
    vol  = ret.std() * np.sqrt(ann)
    sh   = (ret.mean()*ann - RF) / vol if vol > 0 else np.nan
    down = ret[ret<0].std() * np.sqrt(ann)
    so   = (ret.mean()*ann - RF) / down if down > 0 else np.nan
    dd   = (eq - eq.cummax()) / eq.cummax()
    mdd  = dd.min()
    cal  = cagr / abs(mdd) if mdd != 0 else np.nan
    out  = dict(label=label, cagr=cagr, total_ret=eq.iloc[-1]/eq.iloc[0]-1,
                vol=vol, sharpe=sh, sortino=so, calmar=cal, mdd=mdd,
                n_trades=len(tdf) if tdf is not None else 0,
                years=round(yrs,2))
    if tdf is not None and len(tdf) > 0:
        wins = tdf[tdf.pnl > 0]; loss = tdf[tdf.pnl <= 0]
        wr = len(wins) / len(tdf)
        aw = float(wins.pnl.mean()) if len(wins) > 0 else 0
        al = float(loss.pnl.mean()) if len(loss) > 0 else 0
        gw = wins.pnl.sum(); gl = abs(loss.pnl.sum())
        avg_hold = float(tdf['days_held'].mean()) if 'days_held' in tdf.columns else np.nan
        reason_ct = tdf['reason'].value_counts().to_dict() if 'reason' in tdf.columns else {}
        out.update(win_rate=wr, avg_win=aw, avg_loss=al,
                   expectancy=wr*aw+(1-wr)*al,
                   profit_factor=float(gw/gl) if gl > 0 else np.nan,
                   avg_hold_days=avg_hold,
                   exit_reasons=reason_ct)
    return out

def yearly(eq):
    y = eq.resample('A').last().pct_change().dropna()
    y.index = y.index.year
    return {int(k): float(v) for k, v in y.items()}

def monthly(eq):
    m = eq.resample('M').last().pct_change().dropna()
    return {str(k.date()): float(v) for k, v in m.items()}

def rolling_sh(eq, w=126):
    r = eq.pct_change().dropna()
    return r.rolling(w).apply(
        lambda x: (x.mean()*252 - RF)/(x.std()*np.sqrt(252)) if x.std() > 0 else np.nan,
        raw=True)

def regime_bd(eq, qqq_daily):
    sr = eq.pct_change().dropna()
    qr = qqq_daily.pct_change().dropna()
    idx = sr.index.intersection(qr.index)
    sr, qr = sr.loc[idx], qr.loc[idx]
    q200 = qqq_daily.rolling(200).mean().reindex(idx).ffill()
    qi   = qqq_daily.reindex(idx).ffill()
    bull = qi > q200
    def ann(r): return float(r.mean()*252) if len(r) > 5 else float('nan')
    return dict(bull_strat=ann(sr[bull]), bear_strat=ann(sr[~bull]),
                bull_qqq=ann(qr[bull]),  bear_qqq=ann(qr[~bull]),
                bull_days=int(bull.sum()), bear_days=int((~bull).sum()))

def monte_carlo(eq, n=3000, h=252):
    ret = eq.pct_change().dropna()
    mu, sig = ret.mean(), ret.std()
    np.random.seed(42)
    sims  = np.random.normal(mu, sig, (n, h))
    paths = np.cumprod(1 + sims, axis=1)
    final = paths[:, -1]
    rm    = np.maximum.accumulate(paths, axis=1)
    mdd   = (paths/rm - 1).min(axis=1)
    return dict(med_ret=float(np.median(final)-1),
                p5_ret=float(np.percentile(final, 5)-1),
                p95_ret=float(np.percentile(final, 95)-1),
                med_dd=float(np.median(mdd)),
                p5_dd=float(np.percentile(mdd, 5)),
                prob_loss=float((final < 1).mean()),
                prob_dd20=float((mdd < -0.20).mean()))

def factor_reg(strat_ret, factors):
    idx = strat_ret.index
    for f in factors.values(): idx = idx.intersection(f.index)
    if len(idx) < 30: return {}
    y = strat_ret.loc[idx].values
    X = np.column_stack([np.ones(len(idx))] + [f.loc[idx].values for f in factors.values()])
    b, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
    yh = X @ b
    ss_res = ((y-yh)**2).sum(); ss_tot = ((y-y.mean())**2).sum()
    return dict(alpha_ann=float(b[0]*252),
                betas={k: float(v) for k,v in zip(factors.keys(), b[1:])},
                r2=float(1-ss_res/ss_tot) if ss_tot > 0 else 0.0)

# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    print(f"=== Qullamaggie Hourly Backtest ===")
    print(f"Period: {START} → {END}")
    print(f"Universe: {len(UNIVERSE)} tickers\n")

    # 1. QQQ daily for regime
    print("1. QQQ daily data…", flush=True)
    qqq_d_raw = yf.download("QQQ", start=START, end=END,
                             interval="1d", auto_adjust=True, progress=False)
    qqq_daily = qqq_d_raw['Close'].squeeze().dropna()
    regime_s  = make_regime(qqq_daily)
    print(f"   QQQ: {len(qqq_daily)} trading days\n")

    # 2. Hourly data for universe (batch download)
    print("2. Hourly universe data…", flush=True)
    raw_h = download_hourly(UNIVERSE, START, END)

    h_close = extract_field(raw_h, 'Close').ffill()
    h_high  = extract_field(raw_h, 'High').ffill()
    h_low   = extract_field(raw_h, 'Low').ffill()
    h_open  = extract_field(raw_h, 'Open').ffill()
    h_vol   = extract_field(raw_h, 'Volume').fillna(0)

    # Drop tickers with mostly empty data (< 30 days of bars)
    min_bars = 30 * 7  # 30 days × 7 hourly bars
    valid = h_close.columns[h_close.notna().sum() >= min_bars].tolist()
    h_close = h_close[valid]; h_high = h_high[valid]
    h_low   = h_low[valid];   h_open = h_open[valid]; h_vol = h_vol[valid]
    print(f"   Valid tickers: {len(valid)} / {len(UNIVERSE)}")
    print(f"   Hourly bars: {len(h_close)}\n")

    # Localize hourly index to ET for date grouping
    if h_close.index.tz is None:
        h_close.index = h_close.index.tz_localize('UTC').tz_convert('America/New_York')
        h_high.index  = h_high.index.tz_localize('UTC').tz_convert('America/New_York')
        h_low.index   = h_low.index.tz_localize('UTC').tz_convert('America/New_York')
        h_open.index  = h_open.index.tz_localize('UTC').tz_convert('America/New_York')
        h_vol.index   = h_vol.index.tz_localize('UTC').tz_convert('America/New_York')
    else:
        h_close.index = h_close.index.tz_convert('America/New_York')
        h_high.index  = h_high.index.tz_convert('America/New_York')
        h_low.index   = h_low.index.tz_convert('America/New_York')
        h_open.index  = h_open.index.tz_convert('America/New_York')
        h_vol.index   = h_vol.index.tz_convert('America/New_York')

    # 3. Daily aggregates from hourly
    print("3. Building daily aggregates…", flush=True)
    d_close, d_high, d_low, d_vol = hourly_to_daily(h_close, h_high, h_low, h_vol)
    print(f"   Daily bars: {len(d_close)}\n")

    # 4. Setup flags
    print("4. Computing setup flags…", flush=True)
    setup_flags = daily_setup_flags(d_close, d_high, d_low, d_vol)
    # Align regime to daily close dates
    regime_aligned = regime_s.reindex(setup_flags.index).ffill().fillna(False)
    # Apply regime to setup_flags
    setup_flags = setup_flags.multiply(regime_aligned, axis=0)
    n_sig = int(setup_flags.sum().sum())
    print(f"   Setup-qualified stock-days: {n_sig:,}\n")

    # 5. ORB tables
    print("5. Building ORB tables…", flush=True)
    orb_h, orb_l, orb_v = build_orb_table(h_open, h_high, h_low, h_close, h_vol)
    atr_d = atr_daily(d_high, d_low, d_close, ATR_N)
    print(f"   ORB dates: {len(orb_h)}\n")

    # 6. Simulate
    print("6. Running hourly simulation…", flush=True)
    equity, trades_df = simulate_hourly(
        h_open, h_high, h_low, h_close, h_vol,
        d_close, d_high, d_low,
        regime_aligned, setup_flags, orb_h, orb_l, orb_v
    )
    print(f"   Completed: {len(trades_df)} trades, final ${equity.iloc[-1]:,.0f}\n")

    # 7. Benchmarks
    print("7. Benchmarks…", flush=True)
    bm_eq = {}
    for sym in list(BENCHMARKS.keys()) + ["QQQ"]:
        try:
            cl = yf.download(sym, start=START, end=END,
                             interval="1d", auto_adjust=True,
                             progress=False)['Close'].squeeze().dropna()
            bm_eq[sym] = INIT_CAP * cl / cl.iloc[0]
            print(f"   {sym}: OK ({len(cl)} bars)")
        except Exception as e:
            print(f"   {sym}: failed — {e}")

    # 8. Metrics
    print("\n8. Computing metrics…", flush=True)
    sm = perf(equity, trades_df, "Qullamaggie Breakout (Hourly)")
    bm = {s: perf(bm_eq[s].reindex(equity.index).ffill(), None, BENCHMARKS.get(s, s))
          for s in bm_eq}

    # 9. Analytics
    yr_s  = yearly(equity)
    mo_s  = monthly(equity)
    reg_b = regime_bd(equity, qqq_daily)
    mc    = monte_carlo(equity)

    strat_ret = equity.pct_change().dropna()
    factors   = {lbl: bm_eq[sym].pct_change().dropna()
                 for sym, lbl in [('SPY','Market'), ('MTUM','Momentum'), ('QQQ','Tech')]
                 if sym in bm_eq}
    fr = factor_reg(strat_ret, factors)
    rs = rolling_sh(equity).dropna()

    # Summary
    print(f"\n{'='*48}")
    print(f"PERIOD:       {equity.index[0].date()} → {equity.index[-1].date()}")
    print(f"CAGR:         {sm.get('cagr',0)*100:.1f}%")
    print(f"Sharpe:       {sm.get('sharpe',0):.2f}")
    print(f"Sortino:      {sm.get('sortino',0):.2f}")
    print(f"Max DD:       {sm.get('mdd',0)*100:.1f}%")
    print(f"Trades:       {sm.get('n_trades',0)}")
    print(f"Win Rate:     {sm.get('win_rate',0)*100:.1f}%")
    print(f"Profit Factor:{sm.get('profit_factor',0):.2f}")
    print(f"Avg Hold:     {sm.get('avg_hold_days',0):.1f} trading days")
    if 'SPY' in bm:
        print(f"SPY CAGR:     {bm['SPY'].get('cagr',0)*100:.1f}%  Sharpe: {bm['SPY'].get('sharpe',0):.2f}")
    if 'QQQ' in bm:
        print(f"QQQ CAGR:     {bm['QQQ'].get('cagr',0)*100:.1f}%  Sharpe: {bm['QQQ'].get('sharpe',0):.2f}")
    if trades_df is not None and len(trades_df) > 0 and 'reason' in trades_df.columns:
        print(f"Exit reasons: {dict(trades_df.reason.value_counts())}")
    print(f"{'='*48}")

    # Save JSON
    def cvt(o):
        if isinstance(o, (np.floating, float)):
            return None if (math.isnan(o) or math.isinf(o)) else float(o)
        if isinstance(o, (np.integer, int)): return int(o)
        if isinstance(o, bool): return bool(o)
        if isinstance(o, pd.Timestamp): return str(o)
        raise TypeError(type(o))

    results = dict(
        period       = dict(start=START, end=END),
        universe_n   = len(valid),
        strategy     = sm,
        benchmarks   = bm,
        yearly       = yr_s,
        monthly      = mo_s,
        regime       = reg_b,
        monte_carlo  = mc,
        factor_reg   = fr,
        equity       = {str(k): float(v) for k,v in equity.items()},
        rolling_sh   = {str(k): float(v) for k,v in rs.items()},
        bm_equity    = {sym: {str(k): float(v) for k,v in
                              bm_eq[sym].reindex(equity.index).ffill().items()}
                        for sym in bm_eq},
        top_tickers  = (trades_df.groupby('ticker')['pnl'].sum()
                        .sort_values(ascending=False).head(20).to_dict()
                        if trades_df is not None and len(trades_df) > 0 else {}),
    )

    with open("backtest_hourly.json", "w") as f:
        json.dump(results, f, default=cvt, indent=2)
    print("\n✓ Saved backtest_hourly.json")
    return results

if __name__ == "__main__":
    main()
