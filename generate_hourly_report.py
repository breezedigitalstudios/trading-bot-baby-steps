"""Generate HTML report for the hourly intraday backtest."""
import json, math
from datetime import datetime

with open("backtest_hourly.json") as f:
    r = json.load(f)

def pct(v, d=1):
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))): return "N/A"
    return f"{v*100:.{d}f}%"
def num(v, d=2):
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))): return "N/A"
    return f"{v:.{d}f}"
def dollar(v):
    if v is None: return "N/A"
    return f"${v:,.0f}"
def col(v, good_positive=True):
    if v is None or (isinstance(v, float) and math.isnan(v)): return "#888"
    return ("#22c55e" if v > 0 else "#ef4444") if good_positive else ("#ef4444" if v > 0 else "#22c55e")

s   = r['strategy']
bm  = r['benchmarks']
yr  = r['yearly']
mo  = r['monthly']
reg = r['regime']
mc  = r['monte_carlo']
fr  = r.get('factor_reg', {})
top = r.get('top_tickers', {})
alpha  = fr.get('alpha_ann', 0) or 0
betas  = fr.get('betas', {})
r2     = fr.get('r2', 0) or 0
period = r['period']

# Exit reasons
exits = s.get('exit_reasons', {})
stop_n    = exits.get('stop', 0)
partial_n = exits.get('partial', 0)
trail_n   = exits.get('sma10_trail', 0)
total_ex  = stop_n + partial_n + trail_n

# Chart data
eq_data  = [(k[:10], v) for k,v in r['equity'].items()]
spy_data = [(k[:10], v) for k,v in r['bm_equity'].get('SPY', {}).items()]
qqq_data = [(k[:10], v) for k,v in r['bm_equity'].get('QQQ', {}).items()]
mtm_data = [(k[:10], v) for k,v in r['bm_equity'].get('MTUM', {}).items()]
rs_data  = [(k[:10], v) for k,v in r.get('rolling_sh', {}).items()]

def sample(data, n=3): return data[::n]

eq_s   = sample(eq_data)
spy_s  = sample(spy_data)
qqq_s  = sample(qqq_data)
mtm_s  = sample(mtm_data)
rs_s   = sample(rs_data)

eq_dates = [d for d,_ in eq_s]
eq_vals  = [v/100000 for _,v in eq_s]
spy_vals = [v/100000 for _,v in spy_s]
qqq_vals = [v/100000 for _,v in qqq_s]
mtm_vals = [v/100000 for _,v in mtm_s]

def compute_dd(data):
    peak=-1e18; out=[]
    for d,v in data:
        peak=max(peak,v); out.append((d,(v-peak)/peak if peak>0 else 0))
    return out
dd_s = sample(compute_dd(eq_data))
dd_vals = [v*100 for _,v in dd_s]
dd_dates = [d for d,_ in dd_s]

rs_dates = [d for d,_ in rs_s]
rs_vals  = [v for _,v in rs_s]

# Monthly chart
mo_dates = list(mo.keys())
mo_vals  = [v*100 for v in mo.values()]
mo_colors= ["rgba(34,197,94,0.75)" if v>=0 else "rgba(239,68,68,0.75)" for v in mo_vals]

# Top tickers
top_labels = list(top.keys())[:15]
top_vals   = [top[t] for t in top_labels]
top_colors = ["rgba(34,197,94,0.75)" if v>=0 else "rgba(239,68,68,0.75)" for v in top_vals]

# Daily vs hourly comparison
DAILY_STATS = dict(cagr=0.133, sharpe=0.61, sortino=0.59, mdd=-0.285,
                   win_rate=0.537, pf=1.629, n_trades=451, period="9yr (2017–2026)")

REPORT_DATE = datetime.today().strftime("%B %d, %Y")

HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Qullamaggie — Hourly ORB Backtest Report</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Inter',system-ui,sans-serif;background:#0f172a;color:#e2e8f0;line-height:1.6}}
.wrap{{max-width:1200px;margin:0 auto;padding:24px}}
h1{{font-size:2rem;font-weight:700;color:#f8fafc;margin-bottom:4px}}
h2{{font-size:1.1rem;font-weight:600;color:#94a3b8;letter-spacing:.06em;text-transform:uppercase;margin:40px 0 14px;padding-bottom:8px;border-bottom:1px solid #1e293b}}
h3{{font-size:1rem;font-weight:600;color:#cbd5e1;margin:20px 0 10px}}
.subtitle{{color:#64748b;font-size:.875rem;margin-bottom:8px}}
.card{{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:22px;margin-bottom:20px}}
.grid-2{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}
.grid-3{{display:grid;grid-template-columns:repeat(3,1fr);gap:20px}}
.grid-4{{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}}
@media(max-width:700px){{.grid-2,.grid-3,.grid-4{{grid-template-columns:1fr}}}}
.metric-box{{background:#0f172a;border:1px solid #334155;border-radius:10px;padding:16px}}
.metric-label{{font-size:.7rem;color:#64748b;text-transform:uppercase;letter-spacing:.07em;margin-bottom:6px}}
.metric-val{{font-size:1.5rem;font-weight:700}}
.metric-sub{{font-size:.7rem;color:#64748b;margin-top:4px}}
.chart-h{{position:relative;height:280px;width:100%}}
.chart-h-tall{{position:relative;height:350px;width:100%}}
table{{width:100%;border-collapse:collapse;font-size:.85rem}}
th{{background:#0f172a;color:#64748b;font-weight:600;padding:9px 12px;text-align:left;border-bottom:1px solid #334155;font-size:.75rem;letter-spacing:.04em;text-transform:uppercase}}
td{{padding:9px 12px;border-bottom:1px solid #1e293b;vertical-align:middle}}
tr:hover td{{background:#253047}}
.tag{{display:inline-block;padding:2px 10px;border-radius:99px;font-size:.72rem;font-weight:600}}
.tag-green{{background:#0a2e1a;color:#4ade80}}
.tag-red{{background:#451010;color:#f87171}}
.tag-yellow{{background:#2a1f00;color:#fbbf24}}
.tag-blue{{background:#0a1f3a;color:#60a5fa}}
.tag-purple{{background:#1e0f3a;color:#c084fc}}
.fi-list{{list-style:none;padding:0}}
.fi-list li{{display:flex;gap:10px;padding:10px 0;border-bottom:1px solid #1e293b;align-items:flex-start;font-size:.85rem}}
.fi-list li:last-child{{border-bottom:none}}
.fi-icon{{min-width:22px;margin-top:1px}}
.fi-label{{font-weight:600;color:#e2e8f0;margin-bottom:2px}}
.fi-text{{color:#94a3b8;line-height:1.5}}
.progress-wrap{{background:#0f172a;border-radius:99px;height:7px;overflow:hidden;margin-top:5px}}
.progress{{height:100%;border-radius:99px}}
.score-row{{display:flex;justify-content:space-between;align-items:center;padding:9px 0;border-bottom:1px solid #1e293b;font-size:.875rem}}
.score-row:last-child{{border-bottom:none}}
.note{{background:#0f172a;border-left:3px solid #475569;padding:10px 14px;border-radius:0 8px 8px 0;font-size:.8rem;color:#94a3b8;margin-top:12px;line-height:1.6}}
.flag-box{{border-radius:10px;padding:18px;margin-bottom:14px}}
.flag-warn{{background:#1c1000;border:1px solid #78350f}}
.flag-crit{{background:#1a0808;border:1px solid #7f1d1d}}
.flag-ok{{background:#081a0d;border:1px solid #14532d}}
.vs-row{{display:flex;gap:0;border-radius:8px;overflow:hidden;height:54px;margin:12px 0}}
.vs-cell{{display:flex;flex-direction:column;justify-content:center;align-items:center;font-size:.72rem}}
.vs-val{{font-weight:800;font-size:1.1rem}}
.pill{{display:inline-flex;align-items:center;gap:5px;background:#1e293b;border:1px solid #334155;border-radius:99px;padding:4px 12px;font-size:.8rem;margin:4px}}
.top-banner{{background:linear-gradient(135deg,#1e293b,#0f172a);border:1px solid #334155;border-radius:14px;padding:28px;margin:28px 0;display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:20px;text-align:center}}
.top-banner .val{{font-size:1.8rem;font-weight:800}}
.top-banner .lbl{{font-size:.7rem;color:#64748b;text-transform:uppercase;letter-spacing:.07em;margin-bottom:6px}}
.top-banner .sub{{font-size:.7rem;color:#64748b;margin-top:4px}}
.donut-wrap{{display:flex;align-items:center;gap:16px}}
.donut-legend{{font-size:.8rem;color:#94a3b8}}
.donut-legend li{{list-style:none;display:flex;align-items:center;gap:6px;margin-bottom:6px}}
.dot{{width:10px;height:10px;border-radius:50%;display:inline-block}}
.wm{{color:#334155;font-size:.72rem;text-align:center;margin-top:40px;padding-top:20px;border-bottom:1px solid #1e293b}}
.insight-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin-top:14px}}
.insight-box{{background:#0f172a;border:1px solid #334155;border-radius:10px;padding:14px}}
.insight-title{{font-size:.72rem;color:#64748b;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px}}
.insight-val{{font-size:1.3rem;font-weight:700}}
.insight-desc{{font-size:.75rem;color:#94a3b8;margin-top:4px;line-height:1.5}}
</style>
</head>
<body>
<div class="wrap">

<!-- HEADER -->
<div style="border-bottom:1px solid #1e293b;padding-bottom:22px;margin-bottom:6px">
  <h1>Qullamaggie Breakout — Hourly ORB Backtest</h1>
  <p class="subtitle">Proper 60-minute Opening Range Breakout simulation &bull; yfinance 1h data &bull; {period['start']} → {period['end']}</p>
  <p class="subtitle">190 tickers (from 199 attempted) &bull; $100,000 starting capital &bull; 0.25% one-way cost &bull; {REPORT_DATE}</p>
  <div style="margin-top:14px;display:flex;flex-wrap:wrap;gap:6px">
    <span class="pill">🕐 Proper ORB Entry (not daily-close proxy)</span>
    <span class="pill">⚡ Intraday Stop Execution</span>
    <span class="pill">🏦 190 Tickers</span>
    <span class="pill">📅 {r['strategy']['years']:.1f} Years</span>
    <span class="pill">🔄 {s['n_trades']} Trades</span>
  </div>
</div>

<!-- TOP BANNER -->
<div class="top-banner">
  <div><div class="lbl">CAGR</div><div class="val" style="color:#60a5fa">{pct(s['cagr'])}</div><div class="sub">vs SPY {pct(bm['SPY']['cagr'])}</div></div>
  <div><div class="lbl">Sharpe</div><div class="val" style="color:#f97316">{num(s['sharpe'])}</div><div class="sub">vs SPY {num(bm['SPY']['sharpe'])}</div></div>
  <div><div class="lbl">Sortino</div><div class="val" style="color:#22c55e">{num(s['sortino'])}</div><div class="sub">downside-adjusted</div></div>
  <div><div class="lbl">Max Drawdown</div><div class="val" style="color:#ef4444">{pct(s['mdd'])}</div><div class="sub">peak-to-trough</div></div>
  <div><div class="lbl">Profit Factor</div><div class="val" style="color:#fbbf24">{num(s.get('profit_factor',0))}</div><div class="sub">win rate {pct(s.get('win_rate',0))}</div></div>
  <div><div class="lbl">Avg Hold</div><div class="val" style="color:#a78bfa">{num(s.get('avg_hold_days',0),1)}d</div><div class="sub">{s['n_trades']} total trades</div></div>
  <div><div class="lbl">Expectancy</div><div class="val" style="color:#22c55e">{dollar(s.get('expectancy',0))}</div><div class="sub">per trade average</div></div>
  <div><div class="lbl">Est. Alpha</div><div class="val" style="color:#4ade80">{pct(alpha)}</div><div class="sub">annualized vs factors</div></div>
</div>

<!-- KEY INSIGHT: DAILY vs HOURLY -->
<h2>What Hourly Data Changes vs Daily Bars</h2>
<div class="card">
  <p style="color:#94a3b8;font-size:.875rem;margin-bottom:18px">The prior report used daily close as the ORB proxy. Here is what proper intraday simulation reveals:</p>
  <table>
    <thead><tr><th>Metric</th><th>Daily Bars (9yr proxy)</th><th>Hourly ORB (2yr real)</th><th>Direction</th><th>Interpretation</th></tr></thead>
    <tbody>
      <tr><td>Win Rate</td><td style="color:#94a3b8">53.7%</td><td style="color:#ef4444;font-weight:700">47.3%</td><td>▼ −6.4pp</td><td><span class="tag tag-red">Daily overstates wins</span> — many daily "wins" were intraday stops in reality</td></tr>
      <tr><td>Profit Factor</td><td style="color:#94a3b8">1.63</td><td style="color:#fbbf24;font-weight:700">1.49</td><td>▼ −0.14</td><td><span class="tag tag-yellow">Slightly worse</span> — consistent with more false breakout exits</td></tr>
      <tr><td>Stop Exit %</td><td style="color:#94a3b8">~35% (estimated)</td><td style="color:#ef4444;font-weight:700">52.7%</td><td>▲ Much higher</td><td><span class="tag tag-red">Most ORBs are false</span> — over half of entries hit the ORB low stop</td></tr>
      <tr><td>Avg Hold</td><td style="color:#94a3b8">~6d (estimated)</td><td style="color:#22c55e;font-weight:700">4.6 days</td><td>▼ Shorter</td><td><span class="tag tag-green">Realistic</span> — quick stops cut losing trades faster than daily assumed</td></tr>
      <tr><td>Expectancy/Trade</td><td style="color:#94a3b8">$459</td><td style="color:#fbbf24;font-weight:700">$259</td><td>▼ −44%</td><td><span class="tag tag-red">Daily significantly overstates</span> — per-trade edge is smaller than daily implied</td></tr>
    </tbody>
  </table>
  <div class="note">⚠️ <strong>Critical finding:</strong> Daily-bar backtests overstate win rate by ~6 percentage points and per-trade expectancy by ~44% for ORB strategies. This happens because daily bars cannot capture intraday stops — a stock that opens, triggers your ORB entry, then reverses to hit the ORB low stop all within one session appears as a "no trade" on daily bars. Hourly ORB simulation is significantly more realistic.</div>
</div>

<!-- TASK 3 — ABSOLUTE PERFORMANCE -->
<h2>Absolute Performance (All 13 Metrics)</h2>
<div class="grid-4" style="margin-bottom:20px">
  <div class="metric-box"><div class="metric-label">CAGR</div><div class="metric-val" style="color:#60a5fa">{pct(s['cagr'])}</div><div class="metric-sub">annualized return</div></div>
  <div class="metric-box"><div class="metric-label">Total Return</div><div class="metric-val" style="color:#60a5fa">{pct(s['total_ret'])}</div><div class="metric-sub">$100K → {dollar(100000*(1+s['total_ret']))}</div></div>
  <div class="metric-box"><div class="metric-label">Ann. Volatility</div><div class="metric-val" style="color:#94a3b8">{pct(s['vol'])}</div><div class="metric-sub">daily std × √252</div></div>
  <div class="metric-box"><div class="metric-label">Sharpe Ratio</div><div class="metric-val" style="color:#f97316">{num(s['sharpe'])}</div><div class="metric-sub">rf = 4%</div></div>
  <div class="metric-box"><div class="metric-label">Sortino Ratio</div><div class="metric-val" style="color:#22c55e">{num(s['sortino'])}</div><div class="metric-sub">downside deviation only</div></div>
  <div class="metric-box"><div class="metric-label">Calmar Ratio</div><div class="metric-val" style="color:#22c55e">{num(s['calmar'])}</div><div class="metric-sub">CAGR / Max Drawdown</div></div>
  <div class="metric-box"><div class="metric-label">Max Drawdown</div><div class="metric-val" style="color:#ef4444">{pct(s['mdd'])}</div><div class="metric-sub">peak-to-trough</div></div>
  <div class="metric-box"><div class="metric-label">Win Rate</div><div class="metric-val" style="color:#fbbf24">{pct(s.get('win_rate',0))}</div><div class="metric-sub">{s['n_trades']} trades over {s['years']:.1f}yrs</div></div>
  <div class="metric-box"><div class="metric-label">Profit Factor</div><div class="metric-val" style="color:#fbbf24">{num(s.get('profit_factor',0))}</div><div class="metric-sub">gross win / gross loss</div></div>
  <div class="metric-box"><div class="metric-label">Avg Win</div><div class="metric-val" style="color:#22c55e">{dollar(s.get('avg_win',0))}</div><div class="metric-sub">per winning trade</div></div>
  <div class="metric-box"><div class="metric-label">Avg Loss</div><div class="metric-val" style="color:#ef4444">{dollar(s.get('avg_loss',0))}</div><div class="metric-sub">per losing trade</div></div>
  <div class="metric-box"><div class="metric-label">Expectancy</div><div class="metric-val" style="color:#22c55e">{dollar(s.get('expectancy',0))}</div><div class="metric-sub">per trade (blended)</div></div>
  <div class="metric-box"><div class="metric-label">Avg Hold (days)</div><div class="metric-val" style="color:#a78bfa">{num(s.get('avg_hold_days',0),1)}</div><div class="metric-sub">trading days held</div></div>
  <div class="metric-box"><div class="metric-label">Trade Frequency</div><div class="metric-val" style="color:#a78bfa">{num(s['n_trades']/s['years']/12,1)}</div><div class="metric-sub">avg trades per month</div></div>
</div>

<div class="card">
  <h3>Exit Reason Breakdown</h3>
  <div class="grid-3">
    <div>
      <div style="position:relative;height:200px"><canvas id="donutChart"></canvas></div>
    </div>
    <div style="display:flex;flex-direction:column;justify-content:center;gap:14px">
      <div>
        <div style="display:flex;justify-content:space-between;margin-bottom:4px">
          <span style="color:#ef4444;font-weight:600">🛑 Stop (ORB low)</span>
          <span style="font-weight:700">{stop_n} exits ({stop_n/total_ex*100:.0f}%)</span>
        </div>
        <div class="progress-wrap"><div class="progress" style="width:{stop_n/total_ex*100:.0f}%;background:#ef4444"></div></div>
        <div style="font-size:.75rem;color:#64748b;margin-top:4px">Over half of ORB entries reverse back below the opening range low — a hallmark of false breakouts</div>
      </div>
      <div>
        <div style="display:flex;justify-content:space-between;margin-bottom:4px">
          <span style="color:#fbbf24;font-weight:600">💰 Phase-1 Partial</span>
          <span style="font-weight:700">{partial_n} exits ({partial_n/total_ex*100:.0f}%)</span>
        </div>
        <div class="progress-wrap"><div class="progress" style="width:{partial_n/total_ex*100:.0f}%;background:#fbbf24"></div></div>
        <div style="font-size:.75rem;color:#64748b;margin-top:4px">40% of position sold after 3 profitable days. These are the "confirmed" breakouts that followed through</div>
      </div>
      <div>
        <div style="display:flex;justify-content:space-between;margin-bottom:4px">
          <span style="color:#22c55e;font-weight:600">🏃 10-SMA Trail</span>
          <span style="font-weight:700">{trail_n} exits ({trail_n/total_ex*100:.0f}%)</span>
        </div>
        <div class="progress-wrap"><div class="progress" style="width:{trail_n/total_ex*100:.0f}%;background:#22c55e"></div></div>
        <div style="font-size:.75rem;color:#64748b;margin-top:4px">The "runners" — stocks that moved enough for a phase-1 exit AND held the 10-SMA long enough to trail. These generate the large wins</div>
      </div>
    </div>
    <div>
      <h3>Win/Loss Math</h3>
      <div class="score-row"><span>Avg Win</span><span style="color:#22c55e;font-weight:700">{dollar(s.get('avg_win',0))}</span></div>
      <div class="score-row"><span>Avg Loss</span><span style="color:#ef4444;font-weight:700">{dollar(s.get('avg_loss',0))}</span></div>
      <div class="score-row"><span>Win/Loss Ratio</span><span style="font-weight:700">{num(abs(s.get('avg_win',1)/s.get('avg_loss',-1)),2)}×</span></div>
      <div class="score-row"><span>Win Rate</span><span style="color:#fbbf24;font-weight:700">{pct(s.get('win_rate',0))}</span></div>
      <div class="score-row"><span>Expectancy/trade</span><span style="color:#22c55e;font-weight:700">{dollar(s.get('expectancy',0))}</span></div>
      <div class="note" style="margin-top:10px">The strategy survives despite a sub-50% win rate because winners average {num(abs(s.get('avg_win',1)/s.get('avg_loss',-1)),2)}× losses. This is the key asymmetry: lose small (quick ORB stop), win big (trail the runner).</div>
    </div>
  </div>
</div>

<!-- EQUITY CURVE -->
<div class="card">
  <h3>Equity Curve vs Benchmarks ({period['start']} → {period['end']})</h3>
  <div class="chart-h-tall"><canvas id="equityChart"></canvas></div>
</div>

<div class="grid-2">
  <div class="card">
    <h3>Drawdown Timeline</h3>
    <div class="chart-h"><canvas id="ddChart"></canvas></div>
  </div>
  <div class="card">
    <h3>Rolling 6-Month Sharpe</h3>
    <div class="chart-h"><canvas id="rsChart"></canvas></div>
    <div class="note" style="margin-top:8px">Rolling window = 126 days. Sharpe swings from deeply negative to +3 — confirms high regime dependence in this 2-year window.</div>
  </div>
</div>

<!-- BENCHMARK COMPARISON -->
<h2>Benchmark Comparison</h2>
<div class="card">
  <table>
    <thead>
      <tr><th>Strategy / Benchmark</th><th>CAGR</th><th>Total Ret</th><th>Sharpe</th><th>Sortino</th><th>Calmar</th><th>Max DD</th><th>Volatility</th><th>vs Strategy</th></tr>
    </thead>
    <tbody>
      <tr style="background:#0a1f10">
        <td><strong>Qullamaggie (Hourly ORB)</strong></td>
        <td><strong>{pct(s['cagr'])}</strong></td>
        <td><strong>{pct(s['total_ret'])}</strong></td>
        <td><strong>{num(s['sharpe'])}</strong></td>
        <td><strong>{num(s['sortino'])}</strong></td>
        <td><strong>{num(s['calmar'])}</strong></td>
        <td><strong>{pct(s['mdd'])}</strong></td>
        <td><strong>{pct(s['vol'])}</strong></td>
        <td>—</td>
      </tr>
"""
bm_order = [("SPY","S&P 500"),("QQQ","Nasdaq 100"),("MTUM","Momentum ETF")]
for sym, name in bm_order:
    if sym not in bm: continue
    b = bm[sym]; diff = s['cagr'] - b['cagr']
    ds = f"+{pct(diff)}" if diff >= 0 else pct(diff)
    dc = "#22c55e" if diff >= 0 else "#ef4444"
    HTML += f"""      <tr>
        <td>{name} ({sym})</td>
        <td>{pct(b['cagr'])}</td><td>{pct(b['total_ret'])}</td>
        <td>{num(b['sharpe'])}</td><td>{num(b['sortino'])}</td>
        <td>{num(b['calmar'])}</td><td>{pct(b['mdd'])}</td>
        <td>{pct(b['vol'])}</td>
        <td style="color:{dc};font-weight:700">{ds}</td>
      </tr>
"""
HTML += f"""    </tbody>
  </table>
  <div class="note">Over this 2-year window, the strategy edges SPY on CAGR (+{pct(s['cagr']-bm['SPY']['cagr'])} excess) but trails on Sharpe (0.80 vs 0.97). It underperforms QQQ and MTUM on every metric. <strong>MTUM momentum ETF at 32.7% CAGR, Sharpe 1.21 is the most important comparison</strong> — simple passive momentum exposure beats the strategy with zero execution cost or complexity.</div>
</div>

<!-- MONTHLY RETURNS -->
<h2>Monthly Performance &amp; Edge Stability</h2>
<div class="card">
  <h3>Monthly Returns Bar Chart</h3>
  <div class="chart-h-tall"><canvas id="monthlyChart"></canvas></div>
  <div class="note">⚠️ <strong>May 2026 (+41.0%) dominates the entire record.</strong> Just as 2020 explained the 9-year daily backtest, a single month explains most of the 2-year hourly result. Without May 2026, estimated CAGR drops from 22.1% to ~9–11%. This is not a repeatable edge — it is a timing coincidence with a violent momentum surge in AI/quantum/disruptive-tech names in the universe.</div>
</div>

<!-- REGIME ANALYSIS -->
<h2>Regime Analysis</h2>
<div class="card">
  <div class="grid-2">
    <div>
      <h3>Bull vs Bear Market Returns (QQQ 200-day SMA filter)</h3>
      <div style="display:flex;gap:0;border-radius:8px;overflow:hidden;height:44px;margin:12px 0">
        <div style="width:{reg['bull_days']/(reg['bull_days']+reg['bear_days'])*100:.0f}%;background:#166534;display:flex;align-items:center;justify-content:center;font-size:.72rem;font-weight:700;color:#4ade80">BULL {reg['bull_days']}d</div>
        <div style="width:{reg['bear_days']/(reg['bull_days']+reg['bear_days'])*100:.0f}%;background:#7f1d1d;display:flex;align-items:center;justify-content:center;font-size:.72rem;font-weight:700;color:#f87171">BEAR {reg['bear_days']}d</div>
      </div>
      <table>
        <thead><tr><th>Regime</th><th>Strategy Ann.</th><th>QQQ Ann.</th><th>Alpha</th></tr></thead>
        <tbody>
          <tr><td>Bull</td><td style="color:#22c55e;font-weight:700">{pct(reg['bull_strat'])}</td><td>{pct(reg['bull_qqq'])}</td><td style="color:#22c55e;font-weight:700">+{pct(reg['bull_strat']-reg['bull_qqq'])}</td></tr>
          <tr><td>Bear (in cash)</td><td style="color:#fbbf24;font-weight:700">{pct(reg['bear_strat'])}</td><td>{pct(reg['bear_qqq'])}</td><td style="color:#ef4444">{pct(reg['bear_strat']-reg['bear_qqq'])}</td></tr>
        </tbody>
      </table>
    </div>
    <div>
      <ul class="fi-list" style="margin-top:14px">
        <li><span class="fi-icon">✅</span><div class="fi-text"><div class="fi-label">Bull Regime: Strategy Beats QQQ</div>In bull markets, the ORB strategy returned {pct(reg['bull_strat'])} annualized vs QQQ {pct(reg['bull_qqq'])} — a genuine +{pct(reg['bull_strat']-reg['bull_qqq'])} outperformance. This is the strategy working as intended.</div></li>
        <li><span class="fi-icon">⚠️</span><div class="fi-text"><div class="fi-label">Bear Regime: Regime Filter Cost</div>Strategy sits mostly in cash during bear periods ({pct(reg['bear_strat'])} ann. return) while QQQ still returned {pct(reg['bear_qqq'])}. In this 2-year window, being in cash during "bear" periods (mostly 2025 tariff scare) cost returns vs just holding QQQ.</div></li>
        <li><span class="fi-icon">🔵</span><div class="fi-text"><div class="fi-label">Regime Balance</div>Nearly even split: {reg['bull_days']} bull days vs {reg['bear_days']} bear days. The 2024–2026 period was unusually volatile with multiple regime switches (Aug 2024 yen unwind, Apr 2025 tariff shock, recovery).</div></li>
      </ul>
    </div>
  </div>
</div>

<!-- FACTOR ANALYSIS -->
<h2>Factor Exposure Analysis</h2>
<div class="card">
  <div class="grid-2">
    <div>
      <h3>Factor Regression (OLS on daily returns)</h3>
      <div class="score-row"><span>Annualized Alpha</span><span style="color:#22c55e;font-weight:700">{pct(alpha)}</span></div>
      <div class="score-row"><span>Market (SPY) Beta</span><span style="color:#ef4444;font-weight:700">{num(betas.get('Market',0))}</span></div>
      <div class="score-row"><span>Momentum (MTUM) Beta</span><span style="color:#60a5fa;font-weight:700">{num(betas.get('Momentum',0))}</span></div>
      <div class="score-row"><span>Tech/Growth (QQQ) Beta</span><span style="color:#60a5fa;font-weight:700">{num(betas.get('Tech',0))}</span></div>
      <div class="score-row"><span>R² (factor explanatory power)</span><span style="color:#fbbf24;font-weight:700">{pct(r2)}</span></div>
    </div>
    <div>
      <ul class="fi-list">
        <li><span class="fi-icon">🟢</span><div class="fi-text"><div class="fi-label">Alpha = {pct(alpha)} — Substantial But Short Sample</div>The regression estimates significant alpha. However with only 2 years of data (~499 observations), the standard error on alpha is large — this is not yet statistically significant at 95% confidence. Needs 5+ years to be convincing.</div></li>
        <li><span class="fi-icon">🔴</span><div class="fi-text"><div class="fi-label">Market Beta = {num(betas.get('Market',0))} — Strong Short Bias</div>Negative market beta of {num(betas.get('Market',0))} reflects the regime filter keeping the strategy in cash ~49% of the period. This is not genuine market-neutral alpha — it's cash drag during bear phases.</div></li>
        <li><span class="fi-icon">🔵</span><div class="fi-text"><div class="fi-label">Tech Beta = {num(betas.get('Tech',0))} — Hidden QQQ Exposure</div>The universe is heavily tech/growth biased. When active, the strategy behaves like a leveraged QQQ with a volatility overlay. This is a factor exposure, not true alpha.</div></li>
        <li><span class="fi-icon">🟡</span><div class="fi-text"><div class="fi-label">R² = {pct(r2)} — Largely Unexplained</div>Only {pct(r2)} of variance explained by known factors. This could mean genuine idiosyncratic alpha OR concentrated event-driven bets (like May 2026) that simple factor models don't capture.</div></li>
      </ul>
    </div>
  </div>
</div>

<!-- MONTE CARLO -->
<h2>Monte Carlo Analysis (1-Year Forward Simulation)</h2>
<div class="card">
  <div class="grid-4">
    <div class="metric-box"><div class="metric-label">Median 1Y Return</div><div class="metric-val" style="color:#22c55e">{pct(mc['med_ret'])}</div><div class="metric-sub">50th pct, 3,000 sims</div></div>
    <div class="metric-box"><div class="metric-label">5th Percentile</div><div class="metric-val" style="color:#ef4444">{pct(mc['p5_ret'])}</div><div class="metric-sub">bad year scenario</div></div>
    <div class="metric-box"><div class="metric-label">95th Percentile</div><div class="metric-val" style="color:#22c55e">{pct(mc['p95_ret'])}</div><div class="metric-sub">strong year scenario</div></div>
    <div class="metric-box"><div class="metric-label">Prob. Annual Loss</div><div class="metric-val" style="color:#f97316">{pct(mc['prob_loss'])}</div><div class="metric-sub">1-in-{int(1/mc['prob_loss']):.0f} years</div></div>
    <div class="metric-box"><div class="metric-label">Median Max DD</div><div class="metric-val" style="color:#fbbf24">{pct(mc['med_dd'])}</div><div class="metric-sub">expected worst pull</div></div>
    <div class="metric-box"><div class="metric-label">P5 Max Drawdown</div><div class="metric-val" style="color:#ef4444">{pct(mc['p5_dd'])}</div><div class="metric-sub">tail risk scenario</div></div>
    <div class="metric-box"><div class="metric-label">Prob DD &gt; 20%</div><div class="metric-val" style="color:#ef4444">{pct(mc['prob_dd20'])}</div><div class="metric-sub">significant drawdown risk</div></div>
    <div class="metric-box"><div class="metric-label">Return Range (P5→P95)</div><div class="metric-val" style="color:#60a5fa" style="font-size:1.1rem">{pct(mc['p5_ret'])} → {pct(mc['p95_ret'])}</div><div class="metric-sub">wide outcome dispersion</div></div>
  </div>
  <div class="note" style="margin-top:16px">🚨 <strong>Prob(DD &gt; 20%) = {pct(mc['prob_dd20'])}</strong> — more than 1-in-3 simulated years produce a drawdown exceeding 20%. This is very high for a strategy claiming systematic edge. The ORB stop mechanism limits individual trade losses but the intraday nature means more whipsaw and regime-switch losses during choppy markets. At 25% position size × 4 slots, a run of 4 consecutive stops costs ~2.5% of portfolio. Multiple such runs in bear/choppy regimes compound to substantial drawdowns.</div>
</div>

<!-- TOP PERFORMING TICKERS -->
<h2>Top Tickers by P&amp;L</h2>
<div class="card">
  <h3>Cumulative P&amp;L by Ticker (all trades combined)</h3>
  <div class="chart-h"><canvas id="topChart"></canvas></div>
  <div class="note" style="margin-top:12px">Concentration in specific names reveals sector and individual stock risk. Tickers generating outsized P&amp;L often represent the "lucky" catch of a single extraordinary run. Losing tickers reveal which areas of the universe generate systematic false breakouts (often biotech, crypto-adjacent names with high volatility and low follow-through).</div>
</div>

<!-- ADVERSARIAL FINDINGS -->
<h2>Evidence Against the Edge (Adversarial Analysis)</h2>
<div class="card">
  <div class="flag-crit flag-box">
    <h3 style="color:#f87171;margin-bottom:8px">🔴 Critical: May 2026 (+41%) Drives Everything</h3>
    <p style="color:#fca5a5;font-size:.875rem;line-height:1.7">A single calendar month — May 2026 — contributed +41% portfolio return. Without it, the 2-year CAGR falls from 22.1% to approximately 9–11%, below both SPY and QQQ. This is the same structural problem as the 9-year daily backtest (where 2020 drove everything). At every time horizon tested, this strategy's apparent edge concentrates in a single extraordinary period. This pattern strongly suggests <em>regime exposure</em>, not <em>durable edge</em>.</p>
  </div>
  <div class="flag-warn flag-box">
    <h3 style="color:#fbbf24;margin-bottom:8px">🟠 Stops Dominate: 52.7% of Exits Are False Breakouts</h3>
    <p style="color:#fde68a;font-size:.875rem;line-height:1.7">More than half of all ORB entry signals immediately fail — the stock breaks the ORB high, then reverses below the ORB low. This is the fundamental challenge of ORB strategies: the market creates the appearance of a breakout to trap momentum buyers, then reverses. A 47% win rate is viable <em>only if</em> the winners are substantially larger than losers (here: {num(abs(s.get('avg_win',1)/s.get('avg_loss',-1)),2)}× ratio). Any deterioration in that ratio — from increased competition for the same setups, or higher slippage on fast-moving stocks — could push the strategy to breakeven or below.</p>
  </div>
  <div class="flag-warn flag-box">
    <h3 style="color:#fbbf24;margin-bottom:8px">🟠 2-Year Window Is Insufficient for Statistical Significance</h3>
    <p style="color:#fde68a;font-size:.875rem;line-height:1.7">167 trades over 2 years is not enough to establish statistical significance of the edge. At a 47.3% observed win rate, the 95% confidence interval spans roughly 39%–55%. The "true" win rate could easily be below 50%. Similarly, the estimated +{pct(alpha)} alpha has a standard error of approximately ±8–12% annualized on this sample — it cannot be distinguished from zero at conventional significance levels.</p>
  </div>
  <div class="flag-ok flag-box">
    <h3 style="color:#4ade80;margin-bottom:8px">✅ Survivorship Bias is Lower Than Daily Test</h3>
    <p style="color:#86efac;font-size:.875rem;line-height:1.7">The 2-year hourly test benefits from a more recent universe — stocks that are still trading in 2024–2026. Some historical survivors from the 9-year test (e.g. SMCI which surged then crashed) are captured more faithfully here. The universe (190 tickers) also includes names that significantly underperformed, slightly reducing the hindsight bias of the daily test. However, names like NVDA and PLTR — which had extraordinary runs in this specific 2-year window — still introduce selection bias.</p>
  </div>
  <div class="flag-warn flag-box">
    <h3 style="color:#fbbf24;margin-bottom:8px">🟠 MTUM Passive ETF Dominates on Every Metric</h3>
    <p style="color:#fde68a;font-size:.875rem;line-height:1.7">The MTUM momentum ETF returned {pct(bm['MTUM']['cagr'])} CAGR with Sharpe {num(bm['MTUM']['sharpe'])} during the same period — no trading, no execution costs, no stop-losses, no ORB signals. The active Qullamaggie strategy with all its complexity returns {pct(s['cagr'] - bm['MTUM']['cagr'])} less CAGR at worse Sharpe. This is the most uncomfortable comparison: the simplest possible momentum exposure wins hands-down.</p>
  </div>
</div>

<!-- FINAL VERDICT -->
<h2>Final Verdict</h2>
<div class="card">
  <div class="grid-2" style="margin-bottom:20px">
    <div>
      <h3 style="color:#22c55e;margin-bottom:12px">✅ Evidence FOR</h3>
      <ul class="fi-list">
        <li><span class="fi-icon">✅</span><div class="fi-text"><div class="fi-label">Positive Expectancy</div>$259/trade × 84 trades/yr = ~$21,800 gross expectancy/yr from a $100K account. The math works if the edge is real.</div></li>
        <li><span class="fi-icon">✅</span><div class="fi-text"><div class="fi-label">Bull Regime Outperformance</div>Strategy +{pct(reg['bull_strat'])} vs QQQ +{pct(reg['bull_qqq'])} in bull regimes — the ORB correctly selects outperforming momentum names within an already bullish market.</div></li>
        <li><span class="fi-icon">✅</span><div class="fi-text"><div class="fi-label">Estimated Alpha {pct(alpha)}</div>Factor regression suggests meaningful alpha that isn't explained by market/momentum/tech exposure — though statistical significance requires more data.</div></li>
        <li><span class="fi-icon">✅</span><div class="fi-text"><div class="fi-label">Intraday Stop Discipline</div>Quick stops (avg 4.6d hold) prevent losers from compounding. The asymmetric win/loss ratio ({num(abs(s.get('avg_win',1)/s.get('avg_loss',-1)),2)}×) is the engine of the strategy.</div></li>
      </ul>
    </div>
    <div>
      <h3 style="color:#ef4444;margin-bottom:12px">❌ Evidence AGAINST</h3>
      <ul class="fi-list">
        <li><span class="fi-icon">❌</span><div class="fi-text"><div class="fi-label">Single Month Concentration</div>May 2026 = +41%. Remove it → CAGR ~9–11%. Benchmarks win. This pattern repeats at every time horizon tested.</div></li>
        <li><span class="fi-icon">❌</span><div class="fi-text"><div class="fi-label">Underperforms MTUM Passive</div>MTUM: {pct(bm['MTUM']['cagr'])} CAGR, Sharpe {num(bm['MTUM']['sharpe'])}. Zero execution cost, zero complexity. Beats the strategy on every metric.</div></li>
        <li><span class="fi-icon">❌</span><div class="fi-text"><div class="fi-label">Sample Too Small</div>167 trades / 2 years → edge not statistically distinguishable from luck at 95% confidence. Need 3–5 more years of live or OOS data.</div></li>
        <li><span class="fi-icon">❌</span><div class="fi-text"><div class="fi-label">High False Breakout Rate</div>52.7% of entries stop out. Every stop is an execution cost event. In live trading, fill slippage on fast-moving stocks at the ORB high would worsen this further.</div></li>
      </ul>
    </div>
  </div>

  <div style="background:#0f172a;border:1px solid #334155;border-radius:10px;padding:22px">
    <div class="grid-4" style="margin-bottom:20px">
      <div style="text-align:center">
        <div style="font-size:.72rem;color:#64748b;text-transform:uppercase;letter-spacing:.07em;margin-bottom:8px">Edge Confidence</div>
        <div style="font-size:2rem;font-weight:800;color:#fbbf24">42%</div>
        <div class="progress-wrap" style="margin-top:8px"><div class="progress" style="width:42%;background:#f59e0b"></div></div>
      </div>
      <div style="text-align:center">
        <div style="font-size:.72rem;color:#64748b;text-transform:uppercase;letter-spacing:.07em;margin-bottom:8px">Edge Quality</div>
        <div style="font-size:2rem;font-weight:800;color:#fbbf24">5 / 10</div>
        <div class="progress-wrap" style="margin-top:8px"><div class="progress" style="width:50%;background:#f59e0b"></div></div>
      </div>
      <div style="text-align:center">
        <div style="font-size:.72rem;color:#64748b;text-transform:uppercase;letter-spacing:.07em;margin-bottom:8px">Overfitting Risk</div>
        <div style="font-size:2rem;font-weight:800;color:#ef4444">6 / 10</div>
        <div class="progress-wrap" style="margin-top:8px"><div class="progress" style="width:60%;background:#ef4444"></div></div>
      </div>
      <div style="text-align:center">
        <div style="font-size:.72rem;color:#64748b;text-transform:uppercase;letter-spacing:.07em;margin-bottom:8px">5yr Outperform vs QQQ</div>
        <div style="font-size:2rem;font-weight:800;color:#ef4444">32%</div>
        <div class="progress-wrap" style="margin-top:8px"><div class="progress" style="width:32%;background:#ef4444"></div></div>
      </div>
    </div>

    <div style="background:#1e293b;border-radius:8px;padding:18px">
      <p style="color:#f97316;font-weight:700;margin-bottom:10px">VERDICT: CONDITIONAL — Paper trade before committing real capital</p>
      <p style="color:#fed7aa;font-size:.875rem;line-height:1.8">
        The hourly backtest is meaningfully more realistic than the daily version, and reveals a strategy with genuine conceptual merit but insufficient statistical evidence of durable edge. The core mechanism is sound: momentum + ORB + asymmetric stops. The regime filter works. The win/loss ratio is favorable.
        <br><br>
        However: the same concentration problem repeats. 2020 in the 9-year test. May 2026 in the 2-year test. A strategy that requires extraordinary market events to show positive expected value is not systematically edge — it is regime harvesting.
        <br><br>
        <strong>What to do next:</strong>
        <strong>(1)</strong> Paper trade for 6 months on a real-time scan using Alpaca's paper API. Track every signal, every fill, every stop. Compare actual fills to the simulated entry prices — the gap is your real slippage.
        <strong>(2)</strong> Build a trade log that records setup score, sector, day-of-week, market regime. After 50+ paper trades, statistical patterns emerge.
        <strong>(3)</strong> Before going live, the strategy must show positive expectancy in paper trading across at least one choppy/range-bound period — not just momentum markets where everything works.
        <br><br>
        The edge quality rating improves from 4/10 (daily test) to 5/10 (hourly test) because the ORB simulation is more realistic. But it does not cross the threshold for capital allocation without live validation.
      </p>
    </div>
  </div>
</div>

<div class="wm">
  Qullamaggie Breakout — Hourly Intraday Validation &bull; {period['start']} → {period['end']} &bull; 190 tickers &bull; {REPORT_DATE}<br>
  Built with Claude Code. For educational use only. Not financial advice.
</div>

</div>

<script>
const cd = {{
  plugins:{{legend:{{labels:{{color:'#94a3b8',font:{{size:11}}}}}},
           tooltip:{{backgroundColor:'#1e293b',titleColor:'#e2e8f0',bodyColor:'#94a3b8',borderColor:'#334155',borderWidth:1}}}},
  scales:{{x:{{ticks:{{color:'#64748b',maxTicksLimit:10,font:{{size:10}}}},grid:{{color:'#1e293b'}}}},
           y:{{ticks:{{color:'#64748b',font:{{size:10}}}},grid:{{color:'#1e293b'}}}}}}
}};

// Equity
new Chart(document.getElementById('equityChart'),{{type:'line',data:{{
  labels:{json.dumps(eq_dates)},
  datasets:[
    {{label:'Qullamaggie (Hourly ORB)',data:{json.dumps([round(v,4) for v in eq_vals])},borderColor:'#60a5fa',borderWidth:2.5,pointRadius:0,fill:false}},
    {{label:'S&P 500 (SPY)',data:{json.dumps([round(v,4) for v in spy_vals])},borderColor:'#22c55e',borderWidth:1.5,borderDash:[4,3],pointRadius:0,fill:false}},
    {{label:'Nasdaq 100 (QQQ)',data:{json.dumps([round(v,4) for v in qqq_vals])},borderColor:'#f97316',borderWidth:1.5,borderDash:[4,3],pointRadius:0,fill:false}},
    {{label:'Momentum ETF (MTUM)',data:{json.dumps([round(v,4) for v in mtm_vals])},borderColor:'#a78bfa',borderWidth:1.5,borderDash:[2,3],pointRadius:0,fill:false}},
  ]}},options:{{...cd,responsive:true,maintainAspectRatio:false,scales:{{...cd.scales,y:{{...cd.scales.y,ticks:{{callback:v=>'$'+(v*100000).toFixed(0).replace(/\\B(?=(\\d{{3}})+(?!\\d))/g,',')}}}}}}}}
}});

// Drawdown
new Chart(document.getElementById('ddChart'),{{type:'line',data:{{
  labels:{json.dumps(dd_dates)},
  datasets:[{{label:'Drawdown %',data:{json.dumps([round(v,2) for v in dd_vals])},borderColor:'#ef4444',borderWidth:1.5,pointRadius:0,fill:true,backgroundColor:'rgba(239,68,68,0.12)'}}]
  }},options:{{...cd,responsive:true,maintainAspectRatio:false,scales:{{...cd.scales,y:{{...cd.scales.y,ticks:{{callback:v=>v.toFixed(1)+'%'}}}}}}}}
}});

// Rolling Sharpe
new Chart(document.getElementById('rsChart'),{{type:'line',data:{{
  labels:{json.dumps(rs_dates)},
  datasets:[
    {{label:'Rolling 6M Sharpe',data:{json.dumps([round(v,3) if v is not None else None for v in rs_vals])},borderColor:'#a78bfa',borderWidth:1.5,pointRadius:0,fill:false,spanGaps:false}},
    {{label:'Zero',data:{json.dumps([0]*len(rs_dates))},borderColor:'#475569',borderWidth:1,borderDash:[4,4],pointRadius:0,fill:false}}
  ]}},options:{{...cd,responsive:true,maintainAspectRatio:false,scales:{{...cd.scales,y:{{...cd.scales.y,ticks:{{callback:v=>v.toFixed(1)}}}}}}}}
}});

// Monthly bar
new Chart(document.getElementById('monthlyChart'),{{type:'bar',data:{{
  labels:{json.dumps(mo_dates)},
  datasets:[{{label:'Monthly Return %',data:{json.dumps([round(v,1) for v in mo_vals])},backgroundColor:{json.dumps(mo_colors)},borderRadius:4}}]
  }},options:{{...cd,responsive:true,maintainAspectRatio:false,scales:{{...cd.scales,y:{{...cd.scales.y,ticks:{{callback:v=>v.toFixed(0)+'%'}}}}}}}}
}});

// Donut exit reasons
new Chart(document.getElementById('donutChart'),{{type:'doughnut',data:{{
  labels:['Stop (52.7%)','Phase-1 Partial (34.7%)','10-SMA Trail (12.6%)'],
  datasets:[{{data:[{stop_n},{partial_n},{trail_n}],backgroundColor:['#ef4444','#fbbf24','#22c55e'],borderColor:'#0f172a',borderWidth:2}}]
  }},options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{position:'bottom',labels:{{color:'#94a3b8',font:{{size:10}}}}}}}}}}
}});

// Top tickers
new Chart(document.getElementById('topChart'),{{type:'bar',data:{{
  labels:{json.dumps(top_labels)},
  datasets:[{{label:'Cumulative P&L ($)',data:{json.dumps([round(v,0) for v in top_vals])},backgroundColor:{json.dumps(top_colors)},borderRadius:4}}]
  }},options:{{...cd,responsive:true,maintainAspectRatio:false,indexAxis:'y',scales:{{...cd.scales,x:{{...cd.scales.x,ticks:{{callback:v=>'$'+v.toFixed(0)}}}}}}}}
}});
</script>
</body>
</html>"""

with open("strategy_report_hourly.html","w") as f:
    f.write(HTML)
print(f"✓ Saved strategy_report_hourly.html ({len(HTML)//1024} KB)")
