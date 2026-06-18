-- Crypto Signal Bot — database schema (PLAN.md §6)
-- Five tables. SQLite. Single source of truth (FR-DP-1/2).
--   Inputs  (collectors -> engine): market_data, onchain_data, sentiment_data
--   Outputs (engine -> learning loop): signals, trades
-- All statements are CREATE ... IF NOT EXISTS so init is idempotent.

-- ── market_data  (collector -> engine) ──────────────────────────
CREATE TABLE IF NOT EXISTS market_data (
    id                INTEGER PRIMARY KEY,
    ts                INTEGER NOT NULL,   -- candle close time (unix)
    symbol            TEXT    NOT NULL,   -- e.g. BTC/USDT
    timeframe         TEXT    NOT NULL,   -- e.g. 15m, 1h
    open              REAL,               -- OHLC
    high              REAL,
    low               REAL,
    close             REAL,
    volume            REAL,               -- candle volume
    volume_ratio      REAL,               -- volume / 30-day avg
    bid_ask_imbalance REAL,               -- order book pressure
    bb_width          REAL,               -- Bollinger Band width
    rsi               REAL,               -- RSI
    vwap_distance     REAL                -- % from VWAP
);

-- ── onchain_data  (collector -> engine) ─────────────────────────
CREATE TABLE IF NOT EXISTS onchain_data (
    id               INTEGER PRIMARY KEY,
    ts               INTEGER NOT NULL,    -- observation time
    symbol           TEXT    NOT NULL,    -- asset
    exchange_inflow  REAL,                -- USD onto exchanges
    exchange_outflow REAL,                -- USD leaving exchanges
    net_flow         REAL,                -- outflow - inflow
    whale_tx_count   INTEGER,             -- # large transfers in window
    flow_signal      TEXT                 -- accumulation / distribution
);

-- ── sentiment_data  (collector -> engine) ───────────────────────
CREATE TABLE IF NOT EXISTS sentiment_data (
    id              INTEGER PRIMARY KEY,
    ts              INTEGER NOT NULL,     -- observation time
    symbol          TEXT    NOT NULL,     -- asset
    sentiment_score REAL,                 -- -1 bearish ... +1 bullish
    credibility     REAL,                 -- source-weighted 0-1
    novelty         REAL,                 -- new vs recycled 0-1
    mention_count   INTEGER,              -- chatter volume in window
    source          TEXT                  -- reddit / telegram / rss
);

-- ── signals  (engine -> learning loop) ──────────────────────────
CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY,
    ts            INTEGER NOT NULL,       -- when evaluated
    symbol        TEXT    NOT NULL,       -- asset
    market_sub    REAL,                   -- market sub-score 0-100
    onchain_sub   REAL,                   -- on-chain sub-score 0-100
    sentiment_sub REAL,                   -- sentiment sub-score 0-100
    composite     REAL,                   -- weighted total 0-100
    direction     TEXT,                   -- long / none (v1 long-only)
    gate_passed   INTEGER,                -- 0 / 1
    reason        TEXT                    -- human-readable explanation
);

-- ── trades  (engine -> learning loop) — the journal ─────────────
CREATE TABLE IF NOT EXISTS trades (
    id           INTEGER PRIMARY KEY,
    signal_id    INTEGER,                 -- -> signals.id
    symbol       TEXT    NOT NULL,        -- asset
    direction    TEXT,                    -- long (v1 long-only)
    mode         TEXT,                    -- dry / live
    entry_ts     INTEGER,                 -- entry time
    entry_price  REAL,                    -- fill in
    size         REAL,                    -- position size
    stop_loss    REAL,                    -- SL set at entry
    take_profit  REAL,                    -- TP set at entry
    exit_ts      INTEGER,                 -- exit time (null while open)
    exit_price   REAL,                    -- fill out
    exit_reason  TEXT,                    -- which exit fired
    pnl          REAL,                    -- realized P&L after fees
    pnl_pct      REAL,                    -- return %
    status       TEXT,                    -- open / closed
    win          INTEGER,                 -- 1 if pnl > 0 (set at close)
    FOREIGN KEY (signal_id) REFERENCES signals(id)
);
