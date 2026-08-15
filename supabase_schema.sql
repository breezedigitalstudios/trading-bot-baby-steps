-- Trading Bot: Baby Steps — Supabase schema
-- Run this in Supabase → SQL Editor → New query

-- ── Regime ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS regime (
    id              BIGSERIAL PRIMARY KEY,
    generated_at    TIMESTAMPTZ NOT NULL UNIQUE,
    regime          TEXT        NOT NULL,
    regime_reason   TEXT,
    qqq_close       NUMERIC,
    sma10           NUMERIC,
    sma20           NUMERIC,
    sma10_above_sma20   BOOLEAN,
    sma10_slope_5d  NUMERIC,
    sma20_slope_5d  NUMERIC,
    sma10_sloping_up    BOOLEAN,
    sma20_sloping_up    BOOLEAN,
    vix             NUMERIC,
    vix_threshold   NUMERIC,
    vix_elevated    BOOLEAN
);

-- ── Watchlist candidates ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS watchlist_candidates (
    id              BIGSERIAL PRIMARY KEY,
    scan_date       DATE        NOT NULL,
    generated_at    TIMESTAMPTZ,
    symbol          TEXT        NOT NULL,
    close           NUMERIC,
    momentum_22     NUMERIC,
    momentum_67     NUMERIC,
    momentum_126    NUMERIC,
    adr_pct         NUMERIC,
    dollar_volume   NUMERIC,
    UNIQUE (scan_date, symbol)
);

-- ── Setup scores ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS setup_scores (
    id              BIGSERIAL PRIMARY KEY,
    score_date      DATE        NOT NULL,
    generated_at    TIMESTAMPTZ,
    symbol          TEXT        NOT NULL,
    stars           INTEGER,
    close           NUMERIC,
    adr_pct         NUMERIC,
    dollar_volume   NUMERIC,
    momentum_22     NUMERIC,
    momentum_67     NUMERIC,
    momentum_126    NUMERIC,
    ma_aligned      BOOLEAN,
    higher_lows     BOOLEAN,
    range_tightening BOOLEAN,
    narrow_candle   BOOLEAN,
    volume_dryup    BOOLEAN,
    rs_vs_spy_1m    NUMERIC,
    sma10           NUMERIC,
    sma20           NUMERIC,
    sma50           NUMERIC,
    UNIQUE (score_date, symbol)
);

-- ── Trades ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trades (
    id                      TEXT PRIMARY KEY,
    trade_date              DATE,
    symbol                  TEXT,
    stars                   INTEGER,
    orb_high                NUMERIC,
    orb_low                 NUMERIC,
    orb_range               NUMERIC,
    atr                     NUMERIC,
    shares                  INTEGER,
    entry_order_id          TEXT,
    stop_order_id           TEXT,
    stop_price              NUMERIC,
    initial_risk_per_share  NUMERIC,
    circuit_breaker_halved  BOOLEAN,
    status                  TEXT,
    fill_price              NUMERIC,
    fill_date               DATE,
    shares_remaining        INTEGER,
    current_stop            NUMERIC,
    phase1_pnl              NUMERIC,
    phase1_date             DATE,
    exit_price              NUMERIC,
    exit_date               DATE,
    exit_reason             TEXT,
    pnl                     NUMERIC,
    r_multiple              NUMERIC,
    sma_exit_close          NUMERIC,
    sma10_at_exit           NUMERIC,
    created_at              TIMESTAMPTZ
);

-- ── Skips ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS skips (
    id                  TEXT PRIMARY KEY,
    skip_date           DATE,
    symbol              TEXT,
    stars               INTEGER,
    reason              TEXT,
    normalized_reason   TEXT,
    rvol                NUMERIC,
    atr                 NUMERIC,
    orb_high            NUMERIC,
    orb_low             NUMERIC,
    rs_vs_spy_1m        NUMERIC,
    earnings_date       TEXT,
    stops_this_week     INTEGER,
    price               NUMERIC,
    created_at          TIMESTAMPTZ
);

-- ── Funnel daily ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS funnel_daily (
    funnel_date         DATE PRIMARY KEY,
    generated_at        TIMESTAMPTZ,
    regime              TEXT,
    regime_reason       TEXT,
    scan_pass_count     INTEGER,
    setups_5star_count  INTEGER,
    setups_4star_count  INTEGER,
    orders_placed_count INTEGER,
    orders_filled_count INTEGER,
    unprocessed_count   INTEGER
);
