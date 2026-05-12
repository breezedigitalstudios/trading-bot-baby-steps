import json
import os
from typing import Dict, List, Optional
from utils import save_html
import pandas as pd
from datetime import datetime, timezone
from dotenv import load_dotenv
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

API_KEY    = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
if not API_KEY or not SECRET_KEY:
    raise RuntimeError("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env")

data_client    = StockHistoricalDataClient(API_KEY, SECRET_KEY)
SCORES_PATH    = os.path.join(os.path.dirname(__file__), "setup_scores.json")
DASHBOARD_PATH = os.path.join(os.path.dirname(__file__), "dashboard_v2.html")


def fetch_chart_bars(symbols: List[str]) -> pd.DataFrame:
    start = datetime.now(timezone.utc) - pd.Timedelta(days=400)
    req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Day, start=start)
    df  = data_client.get_stock_bars(req).df
    return df[~df.index.duplicated(keep="last")]


def prepare_chart_data(bars: pd.DataFrame, symbols: List[str]) -> Dict:
    out = {}
    for sym in symbols:
        try:
            g = bars.loc[sym].sort_index()
        except KeyError:
            continue
        n     = min(252, len(g))
        g     = g.iloc[-n:]
        close = g["close"]
        sma10 = close.rolling(10).mean()
        sma20 = close.rolling(20).mean()
        sma50 = close.rolling(50).mean()

        def to_js(s):
            return [round(float(v), 2) if pd.notna(v) else None for v in s]

        dates = [str(ts.date()) if hasattr(ts, "date") else str(ts)[:10] for ts in g.index]

        # OHLC as array: [open, close, low, high] for ECharts candlestick
        ohlc = [[
            round(float(row["open"]), 2),
            round(float(row["close"]), 2),
            round(float(row["low"]), 2),
            round(float(row["high"]), 2),
        ] for _, row in g.iterrows()]

        out[sym] = {
            "dates": dates,
            "ohlc": ohlc,
            "volume": [round(float(v), 0) for v in g["volume"]],
            "sma10": to_js(sma10),
            "sma20": to_js(sma20),
            "sma50": to_js(sma50),
        }
    return out


def fmt_momentum(val: Optional[float], period: str) -> str:
    if val is None:
        return ""
    pct = (val - 1) * 100
    if pct >= 1000:
        return f"{pct:,.0f}% · {period}"
    return f"+{pct:.0f}% · {period}"


def plain_summary(symbol: str, c: Dict) -> str:
    b   = c.get("breakdown", {})
    m22 = c.get("momentum_22")
    parts = []

    if m22:
        pct = (m22 - 1) * 100
        parts.append(f"<b>{symbol}</b> is up <b>{pct:,.0f}%</b> from its one-month low")

    if b.get("ma_aligned"):
        parts.append("all three moving averages are stacked in bullish order with price trading above the 20-day SMA")
    else:
        parts.append("the moving averages are not yet fully aligned — watch for a reclaim of the 20-day SMA")

    consol = []
    if b.get("higher_lows"):      consol.append("higher lows")
    if b.get("range_tightening"): consol.append("a tightening range")
    if b.get("narrow_candle"):    consol.append("a narrow candle yesterday")
    if consol:
        parts.append("the consolidation is showing " + ", ".join(consol) + " — demand quietly absorbing supply")

    if b.get("volume_dryup"):
        parts.append("volume is drying up through the base, which means holders are patient and there is no distribution pressure")

    rs = b.get("relative_strength_vs_qqq")
    if rs is True:
        parts.append("it has been outperforming the NASDAQ over the last ten days")
    elif rs is False:
        parts.append("it has been lagging the NASDAQ recently — worth monitoring")

    return ". ".join(parts) + "."


CRITERIA_DEFS = [
    ("ma_aligned",       "Trend alignment"),
    ("higher_lows",      "Higher lows"),
    ("range_tightening", "Range tightening"),
    ("narrow_candle",    "Narrow candle"),
    ("volume_dryup",     "Volume dry-up"),
]


def render_section(c: Dict) -> str:
    sym   = c["symbol"]
    stars = c["stars"]
    close = c.get("close") or 0
    adr   = c.get("adr_pct") or 0
    dvol  = c.get("dollar_volume") or 0
    m22   = c.get("momentum_22")
    m67   = c.get("momentum_67")
    m126  = c.get("momentum_126")
    b     = c.get("breakdown", {})

    star_str = "★" * stars + "☆" * (5 - stars)
    dvol_str = f"${dvol / 1e6:.1f}M"

    momentum_parts = [fmt_momentum(m22, "1 mo"), fmt_momentum(m67, "3 mo"), fmt_momentum(m126, "6 mo")]
    momentum_str   = "   ·   ".join(p for p in momentum_parts if p)

    rs = b.get("relative_strength_vs_qqq")
    rs_str = ""
    if rs is True:
        rs_str = '<span class="rs-up">↑ outperforming QQQ</span>'
    elif rs is False:
        rs_str = '<span class="rs-dn">↓ lagging QQQ</span>'

    sma10 = b.get("sma10") or 0
    sma20 = b.get("sma20") or 0
    sma50 = b.get("sma50") or 0

    def sma_txt(sma, name):
        if not sma:
            return ""
        pct  = (close / sma - 1) * 100
        sign = "+" if pct >= 0 else ""
        return f"{name} ${sma:.2f} ({sign}{pct:.1f}%)"

    sma_str = "   ·   ".join(filter(None, [sma_txt(sma10, "SMA10"), sma_txt(sma20, "SMA20"), sma_txt(sma50, "SMA50")]))

    criteria_html = "   ".join(
        f'<span class="{"crit-pass" if b.get(k) else "crit-fail"}">'
        f'{"✓" if b.get(k) else "–"} {label}</span>'
        for k, label in CRITERIA_DEFS
    )

    summary = plain_summary(sym, c)

    return f"""
<section class="setup">
  <div class="setup-head">
    <div class="setup-left">
      <span class="sym">{sym}</span>
      <span class="stars">{star_str}</span>
    </div>
    <div class="setup-right meta">
      ${close:.2f} &nbsp;·&nbsp; ADR {adr:.1f}% &nbsp;·&nbsp; {dvol_str} avg vol &nbsp;·&nbsp; {rs_str}
    </div>
  </div>

  <div class="momentum meta">{momentum_str}</div>
  <div class="sma-line meta">{sma_str}</div>

  <div class="chart-wrap-price" id="chart-price-{sym}"></div>
  <div class="chart-wrap-vol" id="chart-vol-{sym}"></div>

  <div class="chart-legend">
    <span class="leg-price">■ Green = up, Red = down</span>
    <span class="leg-sma10">── SMA 10</span>
    <span class="leg-sma20">── SMA 20</span>
    <span class="leg-sma50">─ ─ SMA 50</span>
  </div>

  <div class="criteria">{criteria_html}</div>
  <p class="summary">{summary}</p>
</section>"""


def generate():
    if not os.path.exists(SCORES_PATH):
        raise FileNotFoundError("setup_scores.json not found — run setup_detector.py first")

    with open(SCORES_PATH) as f:
        data = json.load(f)

    high_quality  = data.get("high_quality", [])
    total_scored  = data.get("total_scored", 0)
    generated_at  = data.get("generated_at", "")
    count_5 = sum(1 for c in high_quality if c["stars"] == 5)
    count_4 = sum(1 for c in high_quality if c["stars"] == 4)

    try:
        dt       = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        date_str = dt.strftime("%B %d, %Y  %H:%M UTC")
    except Exception:
        date_str = generated_at

    symbols = [c["symbol"] for c in high_quality]

    print(f"Fetching 12-month price data for {len(symbols)} stocks...")
    bars       = fetch_chart_bars(symbols)
    chart_data = prepare_chart_data(bars, symbols)
    print(f"Chart data ready for {len(chart_data)} stocks.")

    sections = "\n".join(render_section(c) for c in high_quality)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Trading Bot — Setups</title>
<style>
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

body {{
  background: #f4f6f9;
  color: #1a2238;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  padding: 56px 0 80px;
  line-height: 1.5;
}}

.container {{
  max-width: 860px;
  margin: 0 auto;
  padding: 0 32px;
}}

.page-header {{
  border-bottom: 1px solid #d1d9e6;
  padding-bottom: 20px;
  margin-bottom: 28px;
}}
.page-header h1 {{
  font-size: 18px;
  font-weight: 600;
  color: #1a2238;
  letter-spacing: .01em;
}}
.page-header .tagline {{
  font-size: 13px;
  color: #8a94a6;
  margin-top: 4px;
}}

.summary-line {{
  font-size: 13px;
  color: #4a5568;
  margin-bottom: 48px;
  letter-spacing: .01em;
}}
.summary-line b {{ color: #1a2238; }}

.setup {{
  padding: 40px 0;
  border-bottom: 1px solid #d1d9e6;
}}
.setup:last-of-type {{ border-bottom: none; }}

.setup-head {{
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 6px;
}}
.setup-left  {{ display: flex; align-items: baseline; gap: 14px; }}

.sym {{
  font-size: 26px;
  font-weight: 700;
  color: #1a2238;
  letter-spacing: .02em;
}}
.stars {{
  font-size: 14px;
  color: #2d4a8a;
  letter-spacing: .1em;
}}

.meta {{
  font-size: 13px;
  color: #8a94a6;
  margin-bottom: 3px;
}}
.setup-right {{ text-align: right; }}

.rs-up {{ color: #2d7a4f; }}
.rs-dn {{ color: #9b2c2c; }}

.momentum {{
  font-size: 13px;
  color: #2d4a8a;
  font-weight: 500;
  margin-bottom: 2px;
}}
.sma-line {{
  margin-bottom: 22px;
  font-size: 12px;
  color: #a0aab8;
}}

.chart-wrap-price {{
  width: 100%;
  height: 210px;
  margin-bottom: 0;
}}
.chart-wrap-vol {{
  width: 100%;
  height: 72px;
  margin-bottom: 10px;
}}

.chart-legend {{
  display: flex;
  gap: 20px;
  font-size: 11px;
  color: #a0aab8;
  margin-bottom: 22px;
  flex-wrap: wrap;
}}
.leg-price {{ color: #1e3a8a; font-weight: 600; }}
.leg-sma10 {{ color: #d97706; }}
.leg-sma20 {{ color: #4a5568; }}
.leg-sma50 {{ color: #a0aab8; }}

.criteria {{
  font-size: 12.5px;
  margin-bottom: 14px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px 18px;
}}
.crit-pass {{ color: #2d7a4f; }}
.crit-fail {{ color: #c0c8d8; }}

.summary {{
  font-size: 13.5px;
  color: #4a5568;
  line-height: 1.7;
  max-width: 720px;
}}
.summary b {{ color: #1a2238; font-weight: 600; }}

.footer {{
  margin-top: 64px;
  padding-top: 40px;
  border-top: 1px solid #d1d9e6;
}}
.footer h2 {{
  font-size: 14px;
  font-weight: 600;
  color: #1a2238;
  letter-spacing: .02em;
  margin-bottom: 20px;
}}
.footer h3 {{
  font-size: 12px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: .07em;
  color: #8a94a6;
  margin-bottom: 14px;
  margin-top: 32px;
}}
.footer h3:first-of-type {{ margin-top: 0; }}
.criterion-ref {{
  display: flex;
  gap: 12px;
  margin-bottom: 16px;
}}
.criterion-ref-name {{
  font-size: 13px;
  font-weight: 600;
  color: #1a2238;
  min-width: 160px;
}}
.criterion-ref-desc {{
  font-size: 13px;
  color: #4a5568;
  line-height: 1.6;
}}
.trade-rule {{
  font-size: 13px;
  color: #4a5568;
  line-height: 1.8;
  margin-bottom: 6px;
}}
.trade-rule b {{ color: #1a2238; font-weight: 600; }}
.trade-rule code {{
  font-family: "SF Mono", "Fira Code", monospace;
  font-size: 12px;
  background: #e8ecf4;
  padding: 1px 5px;
  border-radius: 3px;
  color: #1a2238;
}}

@media (max-width: 600px) {{
  .setup-head {{ flex-direction: column; }}
  .setup-right {{ text-align: left; }}
  .sym {{ font-size: 22px; }}
  .criterion-ref {{ flex-direction: column; gap: 4px; }}
}}
</style>
</head>
<body>
<div class="container">

  <div class="page-header">
    <h1>Trading Bot — High Quality Setups</h1>
    <p class="tagline">Generated {date_str} &nbsp;·&nbsp; Qullamaggie Breakout &nbsp;·&nbsp; Paper Trading</p>
  </div>

  <p class="summary-line">
    <b>{total_scored}</b> candidates scored &nbsp;·&nbsp;
    <b>{len(high_quality)}</b> high quality (≥ 4 stars) &nbsp;·&nbsp;
    <b>{count_5}</b> five-star &nbsp;·&nbsp;
    <b>{count_4}</b> four-star
  </p>

  {sections}

  <div class="footer">
    <h2>Reference</h2>

    <h3>Setup Scoring — 5 Criteria</h3>
    <p class="trade-rule" style="margin-bottom:18px;">Each criterion is worth one point. A setup scoring <b>4 or 5</b> is considered high quality and eligible for trading. All five measure whether the stock has had a clean, controlled pullback and is ready to break out.</p>

    <div class="criterion-ref">
      <span class="criterion-ref-name">✓ Trend alignment</span>
      <span class="criterion-ref-desc">The 10, 20, and 50-day SMAs are stacked in ascending order (SMA10 &gt; SMA20 &gt; SMA50) and price is above the 20-day SMA. Confirms a healthy uptrend with no internal breakdown. This is the single most important criterion — without it, the stock has no business being on the watchlist.</span>
    </div>
    <div class="criterion-ref">
      <span class="criterion-ref-name">✓ Higher lows</span>
      <span class="criterion-ref-desc">The lowest price of the last 5 trading days is above the lowest price of the prior 10 days. Each pullback is shallower than the last — a sign that buyers are absorbing supply at progressively higher levels. This is the hallmark of a stock under accumulation.</span>
    </div>
    <div class="criterion-ref">
      <span class="criterion-ref-name">✓ Range tightening</span>
      <span class="criterion-ref-desc">The average daily high-to-low range over the last 5 sessions is smaller than the average over the prior 10 sessions. Volatility is compressing. Energy is coiling. The narrower the range gets, the more explosive the eventual breakout tends to be.</span>
    </div>
    <div class="criterion-ref">
      <span class="criterion-ref-name">✓ Narrow-range candle</span>
      <span class="criterion-ref-desc">Yesterday's high-to-low range was in the bottom quartile of the past 20 days. A notably quiet session the day before a breakout is a classic pre-breakout signal — the stock is holding its ground with minimal movement, suggesting a decision point is near.</span>
    </div>
    <div class="criterion-ref">
      <span class="criterion-ref-name">✓ Volume dry-up</span>
      <span class="criterion-ref-desc">Average volume over the last 5 days is more than 15% below the prior 15-day average. Sellers have stepped back. When a strong stock pulls back on low volume, it signals the pullback is a rest, not a reversal. High volume during a pullback is a red flag; low volume is a green one.</span>
    </div>

    <h3>What Triggers a Trade</h3>
    <p class="trade-rule"><b>Gate checks (all must be true before the open):</b> setup scores ≥ 4 stars · NASDAQ 10-day SMA above 20-day SMA, both sloping up · fewer than 4 open positions · fewer than 2 new entries today · no earnings within 2–3 days.</p>
    <p class="trade-rule"><b>Entry:</b> buy when price breaks above the high of the first 60-minute candle (9:30–10:30 AM opening range high).</p>
    <p class="trade-rule"><b>Stop:</b> low of the same 60-minute candle. <b>Skip the trade if</b> <code>ORB high − ORB low &gt; 14-day ATR</code> — the risk is too wide.</p>
    <p class="trade-rule"><b>Position size:</b> <code>shares = (account × 10%) ÷ risk/share</code>, capped at <code>account × 25% ÷ entry price</code>.</p>
    <p class="trade-rule"><b>Exit — Phase 1:</b> after 3 full trading days (weekends excluded), if profitable, sell one-third and move the remaining stop to breakeven.</p>
    <p class="trade-rule"><b>Exit — Phase 2:</b> trail remaining shares on the 10-day SMA. Exit on the first daily close below it. Intraday dips that recover by the close do not count.</p>
  </div>

</div>

<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<script>
const chartData = {json.dumps(chart_data)};

for (const [sym, d] of Object.entries(chartData)) {{
  // Price chart with candlesticks + SMAs
  const priceEl = document.getElementById('chart-price-' + sym);
  if (priceEl) {{
    const priceChart = echarts.init(priceEl);
    priceChart.setOption({{
      animation: false,
      grid: [{{ left: '8%', right: '8%', top: 0, bottom: '35%' }}],
      xAxis: [{{ type: 'category', data: d.dates, boundaryGap: true,
        axisLine: {{ lineStyle: {{ color: '#d1d9e6' }} }},
        axisLabel: {{ show: false }},
        splitLine: {{ show: false }},
      }}],
      yAxis: [{{ type: 'value', position: 'right',
        splitLine: {{ lineStyle: {{ color: '#e8ecf2' }} }},
        axisLine: {{ show: false }},
        axisTick: {{ show: false }},
        axisLabel: {{ color: '#a0aab8', fontSize: 10 }},
      }}],
      series: [
        {{ name: 'Price', type: 'candlestick', data: d.ohlc,
          itemStyle: {{ color: '#4ade80', color0: '#f87171', borderColor: '#4ade80', borderColor0: '#f87171' }},
          xAxisIndex: 0, yAxisIndex: 0 }},
        {{ name: 'SMA10', type: 'line', data: d.sma10,
          lineStyle: {{ color: '#d97706', width: 1.5 }}, symbol: 'none',
          xAxisIndex: 0, yAxisIndex: 0 }},
        {{ name: 'SMA20', type: 'line', data: d.sma20,
          lineStyle: {{ color: '#4a5568', width: 1.5 }}, symbol: 'none',
          xAxisIndex: 0, yAxisIndex: 0 }},
        {{ name: 'SMA50', type: 'line', data: d.sma50,
          lineStyle: {{ color: '#a0aab8', width: 1.5, type: 'dashed' }}, symbol: 'none',
          xAxisIndex: 0, yAxisIndex: 0 }},
      ],
      tooltip: {{ trigger: 'axis', backgroundColor: '#1a2238', borderColor: '#2d4a8a',
        textStyle: {{ color: '#e2e8f0', fontSize: 12 }},
        formatter: (params) => {{
          let html = '<div style="line-height:1.6">' + params[0].name + '<br/>';
          for (let p of params) {{
            if (p.componentSubType === 'candlestick') {{
              const ohlc = p.value;
              html += `<span style="color:#1e3a8a">O:$${{ohlc[0]}} H:$${{ohlc[3]}} L:$${{ohlc[2]}} C:$${{ohlc[1]}}</span><br/>`;
            }} else {{
              html += `<span style="color:${{p.color}}">${{p.seriesName}}: $${{p.value?.toFixed(2) || 'N/A'}}</span><br/>`;
            }}
          }}
          return html + '</div>';
        }},
      }},
    }});
  }}

  // Volume chart
  const volEl = document.getElementById('chart-vol-' + sym);
  if (volEl) {{
    const volChart = echarts.init(volEl);
    const volColors = d.ohlc.map((candle) => candle[1] >= candle[0] ? 'rgba(74,222,128,0.6)' : 'rgba(248,113,113,0.6)');
    volChart.setOption({{
      animation: false,
      grid: [{{ left: '8%', right: '8%', top: 0, bottom: 0 }}],
      xAxis: [{{ type: 'category', data: d.dates, boundaryGap: true,
        axisLine: {{ lineStyle: {{ color: '#d1d9e6' }} }},
        axisLabel: {{ color: '#a0aab8', fontSize: 10 }},
        splitLine: {{ show: false }},
      }}],
      yAxis: [{{ type: 'value', position: 'right',
        splitLine: {{ lineStyle: {{ color: '#e8ecf2' }} }},
        axisLine: {{ show: false }},
        axisTick: {{ show: false }},
        axisLabel: {{ color: '#a0aab8', fontSize: 9 }},
      }}],
      series: [{{ name: 'Volume', type: 'bar', data: d.volume,
        itemStyle: {{ color: (params) => volColors[params.dataIndex] }},
        xAxisIndex: 0, yAxisIndex: 0 }}],
      tooltip: {{ trigger: 'axis', backgroundColor: '#1a2238', borderColor: '#2d4a8a',
        textStyle: {{ color: '#e2e8f0', fontSize: 12 }},
        formatter: (params) => `<div style="line-height:1.6">${{params[0].name}}<br/><span style="color:#93aed4">Vol: $${{(params[0].value/1e6).toFixed(2)}}M</span></div>`,
      }},
    }});
  }}
}}
</script>
</body>
</html>"""

    save_html(html, DASHBOARD_PATH)

    print(f"Dashboard written → dashboard_v2.html")
    if os.uname().sysname == "Darwin":
        os.system(f'open "{DASHBOARD_PATH}"')


if __name__ == "__main__":
    generate()
