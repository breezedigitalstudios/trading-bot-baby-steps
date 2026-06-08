"""
Qullamaggie Breakout Strategy — Institutional-Grade Validation (v3)
Vectorized signals, batched downloads, yfinance 1.x compatible.
"""
import warnings; warnings.filterwarnings('ignore')
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime
import json, sys

# ── CONFIG ──────────────────────────────────────────────────────────────────
START      = "2017-01-01"
END        = datetime.today().strftime("%Y-%m-%d")
INIT_CAP   = 100_000.0
TOTAL_COST = 0.0025    # 0.25% one-way (slippage + commission + spread)
MAX_POS    = 0.25
MIN_PRICE  = 20.0
MIN_DVOL   = 20e6
ATR_N      = 14
PARTIAL_FRAC = 0.40
PARTIAL_DAYS = 3
RF         = 0.04

UNIVERSE = [
    "NVDA","AMD","CRWD","PANW","DDOG","NET","ZS","CELH","ENPH","SMCI",
    "AXON","MELI","SHOP","SQ","ROKU","UPST","TTD","PLTR","TSLA","NFLX",
    "META","AMZN","GOOGL","MSFT","AAPL","LULU","DECK","ONON","CROX","FICO",
    "ELF","FTNT","GNRC","PODD","ALGN","VEEV","PAYC","HUBS","ZI","GTLB",
    "COIN","SNOW","BILL","AFRM","RBLX","PINS","MGNI","SE","NTRA","ALNY",
]
BENCHMARKS = {"SPY":"S&P 500","QQQ":"Nasdaq 100","RSP":"Equal-Wt S&P","MTUM":"Momentum"}

# ── DATA FETCH (yfinance 1.x) ────────────────────────────────────────────────
def download_field(tickers, field, start=START, end=END):
    """
    Download a single OHLCV field for multiple tickers.
    yfinance 1.x returns (Price, Ticker) MultiIndex columns.
    Returns DataFrame indexed by date, columns = tickers.
    """
    if isinstance(tickers, str): tickers = [tickers]
    raw = yf.download(tickers, start=start, end=end,
                      auto_adjust=True, progress=False)
    if raw.empty:
        return pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex):
        # yfinance 1.x: top level = field name, second level = ticker
        if field in raw.columns.get_level_values(0):
            df = raw[field]
            if isinstance(df, pd.Series):
                df = df.to_frame(name=tickers[0])
            return df
        # Fallback: old format (ticker, field)
        if field in raw.columns.get_level_values(1):
            return raw.xs(field, axis=1, level=1)
    return pd.DataFrame()

# ── INDICATORS ──────────────────────────────────────────────────────────────
def sma(df, n): return df.rolling(n, min_periods=n).mean()
def ema_s(s, n): return s.ewm(span=n, adjust=False).mean()

def atr_df(h, l, c, n=14):
    """ATR for each column."""
    result = pd.DataFrame(index=h.index, columns=h.columns, dtype=float)
    for col in h.columns:
        tr = pd.concat([h[col]-l[col],
                        (h[col]-c[col].shift()).abs(),
                        (l[col]-c[col].shift()).abs()], axis=1).max(axis=1)
        result[col] = tr.ewm(span=n, adjust=False).mean()
    return result

# ── REGIME ──────────────────────────────────────────────────────────────────
def regime(qqq_cl):
    s10 = qqq_cl.rolling(10).mean()
    s20 = qqq_cl.rolling(20).mean()
    return (s10 > s20) & (s10 > s10.shift(1)) & (s20 > s20.shift(1))

# ── SIGNAL GENERATION ───────────────────────────────────────────────────────
def gen_signals(closes, highs, lows, vols, regime_s):
    at = atr_df(highs, lows, closes, ATR_N)
    dvol = closes * vols

    # Momentum: absolute (not relative) — universe is pre-selected momentum stocks
    # C/Min(L,22) >= 1.10 (at least 10% above 22-day low → in prior uptrend)
    mom22  = closes / lows.rolling(22).min()
    mom126 = closes / lows.rolling(126).min()
    mom_ok = (mom22 >= 1.10) & (mom126 >= 1.25)

    avg_vol = vols.rolling(20).mean()
    breakout = (closes > highs.shift(1)) & (vols > avg_vol * 1.1)
    # Extension: close not more than 1.5 ATR above prior day low
    not_ext  = (closes - lows.shift(1)) <= (1.5 * at)

    s10 = sma(closes, 10); s20 = sma(closes, 20); s50 = sma(closes, 50)
    adr_ = ((highs - lows) / lows.replace(0, np.nan) * 100).rolling(20).mean()

    # Consolidation proxy: stock in uptrend, with some tightening
    higher_lows = lows > lows.shift(1).rolling(5).min()
    setup_ok = (adr_ >= 4) & mom_ok & (s10 > s10.shift(3)) & (closes > s20) & higher_lows
    liq_ok   = (closes >= MIN_PRICE) & (dvol >= MIN_DVOL)

    sig = breakout & not_ext & setup_ok & liq_ok
    sig = sig.multiply(regime_s.reindex(sig.index).ffill().fillna(False), axis=0)
    return sig.fillna(False).astype(bool), at

# ── SIMULATION ──────────────────────────────────────────────────────────────
def simulate(sig, closes, highs, lows, at):
    s10 = sma(closes, 10)
    dates = closes.index
    cash = INIT_CAP
    positions = {}
    trades = []
    equity = np.full(len(dates), np.nan)

    for i, date in enumerate(dates):
        # ── Exits ──
        to_rm = []
        for tk, pos in positions.items():
            if tk not in closes.columns: continue
            cl = closes.iloc[i][tk]; lo = lows.iloc[i][tk]
            sm = s10.iloc[i][tk] if tk in s10.columns else np.nan
            if pd.isna(cl): continue
            days = i - pos['entry_i']
            exit_px = None; reason = None

            if not pd.isna(lo) and lo <= pos['stop']:
                exit_px = max(pos['stop'], lo) * (1 - TOTAL_COST); reason = "stop"
            elif not pos['partial'] and days >= PARTIAL_DAYS and cl > pos['ep']:
                n = int(pos['sh'] * PARTIAL_FRAC)
                if n > 0:
                    ep = cl * (1 - TOTAL_COST)
                    cash += n * ep
                    trades.append(dict(ticker=tk, entry_i=pos['entry_i'], exit_i=i,
                                       ep=pos['ep'], xp=ep, sh=n,
                                       pnl=n*(ep-pos['ep']), reason='partial'))
                    pos['sh'] -= n; pos['partial'] = True; pos['stop'] = pos['ep']
            elif pos['partial'] and not pd.isna(sm) and cl < sm:
                exit_px = cl * (1 - TOTAL_COST); reason = "sma10"

            if exit_px and reason:
                pnl = pos['sh'] * (exit_px - pos['ep'])
                cash += pos['sh'] * exit_px
                trades.append(dict(ticker=tk, entry_i=pos['entry_i'], exit_i=i,
                                   ep=pos['ep'], xp=exit_px, sh=pos['sh'],
                                   pnl=pnl, reason=reason))
                to_rm.append(tk)
        for tk in to_rm: positions.pop(tk, None)

        # ── Mark-to-market ──
        total = cash
        for tk, pos in positions.items():
            px = closes.iloc[i].get(tk, pos['ep'])
            total += pos['sh'] * (px if not pd.isna(px) else pos['ep'])

        # ── Entries ──
        slots = max(0, 4 - len(positions))
        if slots > 0:
            day_sig = sig.iloc[i]
            cands = [t for t in day_sig[day_sig].index if t not in positions
                     and t in closes.columns]
            at_now = at.iloc[i]
            cands.sort(key=lambda t: at_now.get(t, 9999))
            for tk in cands[:slots]:
                ep = closes.iloc[i].get(tk, np.nan)
                at_v = at_now.get(tk, np.nan)
                if pd.isna(ep) or pd.isna(at_v): continue
                pos_sz = min(MAX_POS * total, cash * 0.95)
                cost   = ep * (1 + TOTAL_COST)
                sh     = int(pos_sz / cost)
                if sh < 1 or sh * cost > cash: continue
                cash  -= sh * cost
                positions[tk] = dict(sh=sh, ep=ep, entry_i=i,
                                     stop=ep-at_v, partial=False)

        equity[i] = total + sum(
            pos['sh'] * (closes.iloc[i].get(tk, pos['ep']) or pos['ep'])
            for tk, pos in positions.items()
        ) - cash  # avoid double-count; just use total already computed
        equity[i] = total

    eq = pd.Series(equity, index=dates)
    eq = eq.replace(0, np.nan).ffill().bfill()
    return eq, pd.DataFrame(trades)

# ── METRICS ─────────────────────────────────────────────────────────────────
def perf(eq, tdf, label):
    ret = eq.pct_change().dropna()
    ann = 252; yrs = len(eq)/ann
    cagr = (eq.iloc[-1]/eq.iloc[0])**(1/yrs)-1 if yrs>0 else 0
    vol  = ret.std()*np.sqrt(ann)
    sh   = (ret.mean()*ann-RF)/vol if vol>0 else np.nan
    down = ret[ret<0].std()*np.sqrt(ann)
    so   = (ret.mean()*ann-RF)/down if down>0 else np.nan
    dd   = (eq-eq.cummax())/eq.cummax()
    mdd  = dd.min()
    cal  = cagr/abs(mdd) if mdd!=0 else np.nan
    out  = dict(label=label, cagr=cagr, total_ret=eq.iloc[-1]/eq.iloc[0]-1,
                vol=vol, sharpe=sh, sortino=so, calmar=cal, mdd=mdd,
                n_trades=len(tdf) if tdf is not None else 0)
    if tdf is not None and len(tdf)>0:
        wins = tdf[tdf.pnl>0]; loss = tdf[tdf.pnl<=0]
        wr   = len(wins)/len(tdf)
        aw   = float(wins.pnl.mean()) if len(wins)>0 else 0
        al   = float(loss.pnl.mean()) if len(loss)>0 else 0
        gw   = wins.pnl.sum(); gl = abs(loss.pnl.sum())
        out.update(win_rate=wr, avg_win=aw, avg_loss=al,
                   expectancy=wr*aw+(1-wr)*al,
                   profit_factor=float(gw/gl) if gl>0 else np.nan)
    return out

def yearly(eq):
    y = eq.resample('A').last().pct_change().dropna()
    y.index = y.index.year
    return {int(k):float(v) for k,v in y.items()}

def rolling_sh(eq, w=252):
    r = eq.pct_change().dropna()
    return r.rolling(w).apply(
        lambda x: (x.mean()*252-RF)/(x.std()*np.sqrt(252)) if x.std()>0 else np.nan,
        raw=True)

def regime_bd(eq, qqq):
    sr = eq.pct_change().dropna(); qr = qqq.pct_change().dropna()
    idx = sr.index.intersection(qr.index)
    sr, qr = sr.loc[idx], qr.loc[idx]
    q200 = qqq.rolling(200).mean().reindex(idx).ffill()
    qi   = qqq.reindex(idx).ffill()
    bull = qi > q200
    def ann(r): return float(r.mean()*252) if len(r)>5 else float('nan')
    return dict(bull_strat=ann(sr[bull]), bear_strat=ann(sr[~bull]),
                bull_qqq=ann(qr[bull]),  bear_qqq=ann(qr[~bull]),
                bull_days=int(bull.sum()), bear_days=int((~bull).sum()))

def walk_fwd(eq, n=3):
    fold = len(eq)//(n+1); rows = []
    for i in range(n):
        is_e  = fold*(i+2); oos_s = is_e; oos_e = min(oos_s+fold, len(eq))
        is_q  = eq.iloc[:is_e]; oos_q = eq.iloc[oos_s:oos_e]
        def cagr(e): return float((e.iloc[-1]/e.iloc[0])**(252/len(e))-1) if len(e)>5 else float('nan')
        def sh(e):
            r=e.pct_change().dropna()
            return float((r.mean()*252-RF)/(r.std()*np.sqrt(252))) if r.std()>0 else float('nan')
        rows.append(dict(fold=i+1, is_cagr=cagr(is_q), oos_cagr=cagr(oos_q),
                         is_sharpe=sh(is_q), oos_sharpe=sh(oos_q)))
    return rows

def monte_carlo(eq, n=2000, h=252):
    ret = eq.pct_change().dropna()
    mu, sig = ret.mean(), ret.std()
    np.random.seed(42)
    sims = np.random.normal(mu, sig, (n, h))
    paths = np.cumprod(1+sims, axis=1)
    final = paths[:,-1]
    rm    = np.maximum.accumulate(paths, axis=1)
    mdd   = (paths/rm-1).min(axis=1)
    return dict(med_ret=float(np.median(final)-1), p5_ret=float(np.percentile(final,5)-1),
                p95_ret=float(np.percentile(final,95)-1), med_dd=float(np.median(mdd)),
                p5_dd=float(np.percentile(mdd,5)), prob_loss=float((final<1).mean()),
                prob_dd20=float((mdd<-0.20).mean()))

def factor_reg(strat_ret, factors):
    idx = strat_ret.index
    for f in factors.values(): idx = idx.intersection(f.index)
    if len(idx) < 50: return {}
    y = strat_ret.loc[idx].values
    X = np.column_stack([np.ones(len(idx))]+[f.loc[idx].values for f in factors.values()])
    b,_,_,_ = np.linalg.lstsq(X,y,rcond=None)
    yh = X@b; ss_res=((y-yh)**2).sum(); ss_tot=((y-y.mean())**2).sum()
    return dict(alpha_ann=float(b[0]*252),
                betas={k:float(v) for k,v in zip(factors.keys(),b[1:])},
                r2=float(1-ss_res/ss_tot) if ss_tot>0 else 0.0)

# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    print("=== Qullamaggie Strategy Validation v3 ===")

    # 1. QQQ regime
    print("1. Downloading QQQ…", flush=True)
    qqq_cl = download_field(["QQQ"], "Close")["QQQ"].dropna()
    reg_s  = regime(qqq_cl)
    print(f"   QQQ: {len(qqq_cl)} bars", flush=True)

    # 2. Universe — all at once (yfinance 1.x handles large batches well)
    print(f"2. Downloading {len(UNIVERSE)} tickers…", flush=True)
    closes = download_field(UNIVERSE, "Close").ffill().dropna(how='all', axis=1)
    highs  = download_field(UNIVERSE, "High").ffill().reindex(columns=closes.columns)
    lows   = download_field(UNIVERSE, "Low").ffill().reindex(columns=closes.columns)
    vols   = download_field(UNIVERSE, "Volume").fillna(0).reindex(columns=closes.columns)
    tickers = closes.columns.tolist()
    print(f"   {len(tickers)} tickers, {len(closes)} days", flush=True)

    # 3. Signals
    print("3. Generating signals…", flush=True)
    sig, at = gen_signals(closes, highs, lows, vols, reg_s)
    print(f"   {int(sig.sum().sum())} total entry signals", flush=True)

    # 4. Simulate
    print("4. Simulating portfolio…", flush=True)
    equity, trades_df = simulate(sig, closes, highs, lows, at)
    print(f"   {len(trades_df)} trades, final ${equity.iloc[-1]:,.0f}", flush=True)

    # 5. Benchmarks
    print("5. Benchmarks…", flush=True)
    bm_eq = {}
    for sym in list(BENCHMARKS.keys()) + ["QQQ"]:
        try:
            cl = download_field([sym], "Close")[sym].dropna()
            bm_eq[sym] = INIT_CAP * cl / cl.iloc[0]
            print(f"   {sym} OK ({len(cl)} bars)", flush=True)
        except Exception as e:
            print(f"   {sym} failed: {e}", flush=True)

    # 6. Metrics
    print("6. Computing metrics…", flush=True)
    sm = perf(equity, trades_df, "Qullamaggie Breakout")
    bm = {s: perf(bm_eq[s].reindex(equity.index).ffill(), None, BENCHMARKS.get(s,s))
          for s in bm_eq}

    # 7. Analytics
    print("7. Analytics…", flush=True)
    yr_s  = yearly(equity)
    yr_sp = yearly(bm_eq['SPY']) if 'SPY' in bm_eq else {}
    reg_b = regime_bd(equity, qqq_cl)
    wf    = walk_fwd(equity)
    mc    = monte_carlo(equity)

    strat_ret = equity.pct_change().dropna()
    factors   = {lbl: bm_eq[sym].pct_change().dropna()
                 for sym,lbl in [('SPY','Market'),('MTUM','Momentum'),('QQQ','Tech')]
                 if sym in bm_eq}
    fr = factor_reg(strat_ret, factors)
    rs = rolling_sh(equity).dropna()

    # Print summary
    print(f"\n{'='*40}")
    print(f"CAGR:          {sm['cagr']*100:.1f}%")
    print(f"Sharpe:        {sm['sharpe']:.2f}")
    print(f"Sortino:       {sm['sortino']:.2f}")
    print(f"Max Drawdown:  {sm['mdd']*100:.1f}%")
    print(f"Trades:        {sm['n_trades']}")
    if 'win_rate' in sm:
        print(f"Win Rate:      {sm['win_rate']*100:.1f}%")
        print(f"Profit Factor: {sm.get('profit_factor','N/A')}")
    if 'SPY' in bm:
        print(f"SPY CAGR:      {bm['SPY']['cagr']*100:.1f}%  Sharpe: {bm['SPY']['sharpe']:.2f}")
    if 'QQQ' in bm:
        print(f"QQQ CAGR:      {bm['QQQ']['cagr']*100:.1f}%  Sharpe: {bm['QQQ']['sharpe']:.2f}")
    print(f"{'='*40}")

    # Save
    def cvt(o):
        if isinstance(o, (np.floating, float)): return None if np.isnan(o) else float(o)
        if isinstance(o, (np.integer, int)):    return int(o)
        if isinstance(o, bool):                 return bool(o)
        if isinstance(o, pd.Timestamp):         return str(o)
        raise TypeError(type(o))

    results = dict(
        strategy     = sm,
        benchmarks   = bm,
        yearly_strat = yr_s,
        yearly_spy   = yr_sp,
        regime       = reg_b,
        walk_forward = wf,
        monte_carlo  = mc,
        factor_reg   = fr,
        equity       = {str(k): float(v) for k,v in equity.items()},
        rolling_sh   = {str(k): float(v) for k,v in rs.tail(600).items()},
        bm_equity    = {sym: {str(k): float(v) for k,v in
                              bm_eq[sym].reindex(equity.index).ffill().items()}
                        for sym in bm_eq},
    )
    with open("backtest_results.json","w") as f:
        json.dump(results, f, default=cvt, indent=2)
    print("\n✓ Saved backtest_results.json")

if __name__ == "__main__":
    main()
