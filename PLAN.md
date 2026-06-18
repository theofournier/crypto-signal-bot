# Crypto Signal & Trading Bot — Technical Plan

> **Spec-Driven Development artifact: the PLAN (the *how*).**
> This document defines **how** the system is built — architecture, components, schema, and
> tech. It implements the requirements in `SPEC-REQUIREMENTS.md` (the *what & why*) and is
> realized by the ordered work items in `BUILD_PLAN.md` (the *tasks*). Read the requirements
> spec first; when a requirement ID (e.g. `FR-SF-2`) is referenced here, it points there.
>
> | SDD layer | File | Answers |
> |---|---|---|
> | Spec | `SPEC-REQUIREMENTS.md` | What & why — requirements, acceptance criteria |
> | **Plan** (this file) | `PLAN.md` | How — architecture, schema, tech stack |
> | Tasks | `BUILD_PLAN.md` | In what order — phased work items |
>
> Intended to be read by Claude Code (and humans) as context before writing code. When in
> doubt about *how*, this file wins; when in doubt about *what/why*, the requirements spec wins.
>
> **Personal, self-hosted, open-source learning project. Not financial advice. Trading risks
> total loss. Everything is validated in dry-run before any real capital is used.**

---

## 1. Runtime shape

(Purpose, users, scope, and non-goals are defined in `SPEC-REQUIREMENTS.md` §1–§5 and not
repeated here.) Implementation realizes that as **two long-running processes plus one database
file**:

- `run_collectors.py` — the writers (collect → store)
- `run_engine.py` — the reader + decision-maker + executor (score → risk → execute → monitor)

No web server, no DB server, no cloud bill. Target market is **crypto** (24/7, free real-time
data, and an on-chain layer that equities lack).

**Position model: long-or-flat (v1).** Spot only — the system holds either the asset (**long**)
or stablecoin (**flat**). It does **not** short. A bearish consensus means "do not enter / close
any open long," never "open a short." Shorting is deferred to a future futures-based version.
(Resolves `SPEC-REQUIREMENTS.md` FR-EX-4 / FR-SG-1.)

---

## 2. Implementation principles (non-negotiable)

The rules every implementation decision must respect. These operationalize the requirements;
where a requirement governs, its ID is noted.

1. **Collectors observe, the brain decides** (FR-DC-5). Collectors only write observations; they
   never trade. Any collector can be restarted/rewritten/removed without touching decision logic.

2. **One database is the single source of truth** (FR-DP-2). Components never call each other
   directly — writers and reader communicate only through the DB. This gives fault tolerance and
   free backtesting.

3. **Only act on completed data** (FR-DC-6). Never decide on a still-forming candle ("repainting"
   produces fake backtests). Only closed candles enter the decision path.

4. **Backtest → dry-run → live, never skip a step** (FR-SF-1/3). One flag switches simulated vs
   real execution; everything else is byte-for-byte identical across modes.

5. **Require agreement, prefer fewer trades** (FR-SG-2). A signal fires only when active sources
   agree on direction. Fewer, higher-conviction trades beat noise.

6. **Public framework, private edge** (FR-OS-1/2). Code is open source; tuned parameters,
   secrets, trained models, and the journal are gitignored and never versioned.

7. **Fees on every fill** (FR-EX-3). Deduct fees in dry-run, backtest, and live; store P&L net of
   fees; prefer maker/limit orders. A fee-free backtest lies.

---

## 3. Architecture overview

```
        ┌──────────────┐   ┌─────────────┐   ┌──────────────────┐
        │ 1 Collectors │──▶│  2 Storage  │──▶│  3 Scoring engine │
        │ mkt/chain/sen│   │   SQLite    │   │   fuse + gate     │
        └──────────────┘   └─────────────┘   └──────────────────┘
                                                       │
                                                       ▼
        ┌──────────────┐   ┌─────────────┐   ┌──────────────────┐
        │ Trade journal│◀──│ 5 Execution │◀──│   4 Risk gate     │
        │  (trades tbl)│   │ place+manage│   │   size or block   │
        └──────────────┘   └─────────────┘   └──────────────────┘
                │
                ▼
        ┌─────────────────────────────────────────┐
        │ 6 Learning loop                          │
        │ postmortem → tune weights → recalibrate ─┼──▶ back into scoring (↻)
        └─────────────────────────────────────────┘
```

Data flows clockwise. The feedback arrow (learning loop → scoring) is what makes the system
improve rather than repeat itself forever.

---

## 4. Project structure

```
crypto-signal-bot/
├── collectors/                # subsystem 1 — observe, write to DB
│   ├── base_collector.py      # shared poll/normalize/write loop
│   ├── market_collector.py    # candles, volume, spread, indicators
│   ├── onchain_collector.py   # flows, whale transfers
│   └── sentiment_collector.py # Reddit/TG/RSS + LLM classify
├── data/                      # subsystem 2 — storage
│   ├── db.py                  # SQLite connection helpers
│   ├── schema.sql             # table definitions (section 6)
│   └── seed.py                # bulk historical loader (section 7)
├── core/                      # subsystems 3 & 4 — the brain
│   ├── normalize.py           # raw features → 0–100 sub-scores
│   ├── scoring.py             # weighted composite + gate
│   └── risk.py                # checks + fractional-Kelly sizing
├── exchange/
│   └── client.py              # CCXT wrapper + dry-run switch
├── execution/                 # subsystem 5
│   ├── executor.py            # place entry + SL/TP atomically
│   └── monitor.py             # watch open positions, race exits
├── learning/                  # subsystem 6
│   ├── postmortem.py          # analyze closed trades, suggest tweaks
│   └── backtest.py            # replay DB rows through scoring.py
├── notifications/
│   └── telegram.py            # DM signals + summaries
├── config/
│   ├── config.example.yaml    # PUBLIC template — safe defaults, committed
│   ├── config.yaml            # PRIVATE — tuned weights, gitignored
│   └── secrets.env            # PRIVATE — API keys, gitignored
├── scripts/
│   ├── run_collectors.py      # launch all collectors
│   ├── run_engine.py          # score → risk → execute loop
│   └── seed_data.py           # one-off historical import
├── tests/                     # incl. lookahead/leakage checks
├── storage.db                 # SQLite file (gitignored)
├── requirements.txt
└── README.md
```

---

## 5. Subsystem details

### 5.1 Collectors

Three independent programs, each on its own clock, sharing a base pattern:

```python
class BaseCollector:
    def run(self):
        while True:
            raw  = self.fetch()         # talk to the source
            rows = self.normalize(raw)  # shape into table columns
            self.write(rows)            # insert into SQLite
            time.sleep(self.interval)   # each subclass sets its own pace
```

- **Market collector** (fast, every few seconds): subscribes to Binance/Bybit WebSocket,
  processes only *closed* candles. Computes: `volume_ratio` (vs 30d avg), `bid_ask_imbalance`,
  `bb_width`, `rsi`, `vwap_distance`. One row per pair per interval.
- **On-chain collector** (slow, every few minutes — APIs rate-limit): pulls exchange
  inflow/outflow and large transfers from DefiLlama/Etherscan/etc. Reduces to a directional
  reading (accumulation/distribution).
- **Sentiment collector** (nuanced): pulls Reddit (PRAW) / Telegram (Telethon) / RSS, runs
  each item through a classifier (small local model via Ollama, e.g. CryptoBERT, OR a
  rule-based scorer) producing `sentiment_score`, `credibility`, `novelty`. Writes a rolling
  aggregate, not individual posts.

**Rule:** a collector NEVER trades. It only writes rows.

### 5.2 Storage

One SQLite file. Three input tables (written by collectors, read by engine) + two output
tables (written by engine, read by learning loop). Full schema in section 6.

### 5.3 Scoring engine

Two stages.

**Stage 1 — Normalization** (`core/normalize.py`): each source's raw features are squashed
onto a common **0–100 sub-score + a direction** (`long`/`bearish`/`none`). This makes them
comparable (a 3.2× volume ratio, a 0.74 sentiment, and a +$340M flow can't be averaged raw).

**Stage 2 — Composite + gate** (`core/scoring.py`). **Long-or-flat model:** the engine acts
only on `long` agreement; a `bearish` consensus produces no entry (and triggers a signal-exit
on any open long, see §5.5) — it never opens a short.

```python
composite = (0.40 * market_sub
           + 0.25 * onchain_sub
           + 0.35 * sentiment_sub)

agree_long = (market_dir == onchain_dir == sentiment_dir == "long")

if composite >= THRESHOLD and agree_long:   # THRESHOLD default 72
    emit_signal(symbol, direction="long", score=composite)
else:
    discard()   # no entry (and close any open long if consensus turned bearish)
```

- Weights are config-driven (see section 9). Market weighted highest (hard data); sentiment
  lowest (noisy).
- The gate requires **both** a high score **and** unanimous `long` agreement. This is the primary
  noise filter. A score of 80 driven only by sentiment while price disagrees = a pump, not an
  edge → must not fire.
- v1 is spot/long-only (FR-EX-4): there is no short path. Direction is effectively `long` or
  `no-action`.

**Rules → ML → LLM (which is the "engine"?)** — these are *stages*, not rivals:
- Start with the **rules** above (transparent, instant, needs no historical data).
- The rules generate the journal that an **ML model** (XGBoost/LightGBM, FreqAI-style) later
  trains on to find nonlinear interactions and self-retrain. Only adopt ML once hundreds of
  trades are logged.
- The **LLM never lives in the scoring step.** It lives upstream in the sentiment collector,
  turning text into the numbers that feed scoring. Using an LLM to combine three numbers
  would be slower, costlier, and worse than arithmetic.

### 5.4 Risk gate (`core/risk.py`)

Runs veto checks, then sizes. Any check can block (blocking is a fine, logged outcome).

1. **Exposure & drawdown check** — already holding too much, or in a drawdown that should
   pause new entries? → block.
2. **Volatility filter** — market in a chaotic spike? → sit out.
3. **Position sizing** — only if both pass. Use **fractional Kelly**:

```python
edge_fraction = win_rate - (1 - win_rate) / reward_risk_ratio
size = bankroll * KELLY_FRACTION * max(edge_fraction, 0)
# win_rate & reward_risk_ratio are read from the trades table → self-calibrating
```

### 5.5 Execution (`execution/`)

**Atomic entry** — protective orders go in WITH the entry, never after (a crash in the gap
leaves an unprotected position). **Prefer LIMIT (maker) entries over market (taker) orders** —
on most exchanges the maker fee is roughly half the taker fee, which compounds enormously over
many trades (see §5.6):

```python
def open_trade(signal, size, sl, tp):
    # v1 is long-only: signal.direction is always "long" (buy)
    # limit order = maker = lower fee; may not fill, which is acceptable
    entry = exchange.create_limit_order(signal.symbol, "buy", size, signal.entry_price)
    exchange.set_stop_loss(signal.symbol, sl)     # immediately
    exchange.set_take_profit(signal.symbol, tp)   # immediately
    db.record_trade(signal.id, entry, size, sl, tp, status="open")
```

**The dry-run switch lives in `exchange/client.py`.** When `config.mode.dry_run == true`,
`create_order` simulates a fill and logs it instead of hitting the API. Everything else
(scoring, risk, journaling) is identical. This is how the whole loop is validated risk-free.

**Exit logic — five exits race; first to fire wins.** All defined at entry, never improvised.

| Exit          | Fires when                                                            | Placed as                                  |
|---------------|-----------------------------------------------------------------------|--------------------------------------------|
| Stop-loss     | Price hits "I was wrong" level (fixed % or ATR/volatility-based)       | Real exchange order — fires even if bot off |
| Take-profit   | Price hits target set by reward:risk ratio (e.g. 2:1); optional scale-out | Real exchange order                     |
| Trailing stop | Price reverses after moving in favor; stop moves up, never down       | Monitor loop adjusts each cycle            |
| Signal exit   | Entry thesis no longer holds — long consensus breaks / turns bearish / score collapses | Monitor loop, re-running scoring on open positions |
| Time exit     | **Hard cap:** `max_hold_hours` (default 48h) elapses. Always active — no indefinite holds (FR-MX-5) | Monitor loop |

### 5.6 Fees (critical — fees can turn a winning strategy into a losing one)

Every trade pays a fee **twice** — once on entry, once on exit. A round trip at 0.10% per side
costs ~0.20%, so a trade must clear ~0.20% just to break even before any profit. For an
automated system doing many trades, fees are often the single largest recurring cost and the
most common reason a "profitable" backtest loses money live.

**Three rules, all mandatory:**

1. **Maker over taker.** Enter and exit with limit orders (maker) wherever possible — maker
   fees are ~half of taker fees on most exchanges (e.g. Binance ~0.075–0.10%). Market orders
   (taker) only when immediacy is worth the higher cost.

2. **Fees are deducted on every fill — simulated AND real.** The dry-run simulator in
   `exchange/client.py` MUST subtract the configured fee from each simulated fill exactly as a
   real exchange would. Otherwise dry-run and backtest results are fiction. Store the realized
   `pnl` in `trades` **net of fees**.

3. **Fees enter the decision, not just the bookkeeping.** The risk gate / scoring should treat
   a signal's expected move as *net of the round-trip fee*. A setup whose expected gain barely
   exceeds `2 × fee_pct` is not worth taking — the fee eats the edge. Filter these out.

```python
# applied in exchange/client.py on every fill (dry and live)
fee = fill_price * size * config.fees.fee_pct
# entry: cash_out = price*size + fee ;  exit: cash_in = price*size - fee
# trades.pnl must be the value AFTER both fees are subtracted
```

Fee rates are config-driven (`config.fees`, see §9) so you can match your exchange's tier and
re-tune as your volume lowers the rate. Withdrawal/deposit fees are separate and one-off; they
don't affect per-trade P&L but should be remembered when sizing capital moves.

### 5.7 Learning loop (`learning/`)

- **When a trade is won/lost:** only at *close*. The monitor loop detects the close, fetches
  the real fill, computes P&L after fees, writes it to `trades` (sets `status=closed`, `win`,
  `pnl`, `exit_reason`). That write makes the trade learnable.
- **One trade tells you nothing.** Judge the *system* on a sample of closed trades, on four
  metrics: **win rate**, **avg win ÷ avg loss**, **net P&L after fees**, **max drawdown**. A
  system "wins" when net P&L > 0 AND drawdown was survivable. **Sample-size rule (FR-LE-4):**
  do not treat metrics as a real verdict below **100 closed trades**; treat **30–100** as a weak
  preliminary read only; below 30 is noise.
- **Backtest vs dry-run consistency (G4):** results are "consistent" when win rate agrees within
  **±5 percentage points** and net result has the **same sign and is within ~20%**. A larger gap
  signals a bug (usually look-ahead leakage) → stop and investigate before trusting either.
- **Postmortem** reads closed trades and asks: on losers, which sources pointed the wrong
  way? If losers consistently had strong sentiment but weak market structure → lower the
  sentiment weight. Loop: new weights → better decisions → new trades → new data → repeat.
- **Manual first** (a weekly summary you read and act on), automated/ML later.
- **Do all of this in dry-run first** — the loop runs identically with simulated trades, so
  you can learn whether the system has an edge before risking a cent.

---

## 6. Database schema

Five tables. SQLite. (`data/schema.sql`)

### market_data  (collector → engine)
| Column | Type | Meaning |
|---|---|---|
| id | INTEGER PK | row id |
| ts | INTEGER | candle close time (unix) |
| symbol | TEXT | e.g. BTC/USDT |
| timeframe | TEXT | e.g. 15m, 1h |
| open, high, low, close | REAL | OHLC |
| volume | REAL | candle volume |
| volume_ratio | REAL | volume ÷ 30-day avg |
| bid_ask_imbalance | REAL | order book pressure |
| bb_width | REAL | Bollinger Band width |
| rsi | REAL | RSI |
| vwap_distance | REAL | % from VWAP |

### onchain_data  (collector → engine)
| Column | Type | Meaning |
|---|---|---|
| id | INTEGER PK | row id |
| ts | INTEGER | observation time |
| symbol | TEXT | asset |
| exchange_inflow | REAL | USD onto exchanges |
| exchange_outflow | REAL | USD leaving exchanges |
| net_flow | REAL | outflow − inflow |
| whale_tx_count | INTEGER | # large transfers in window |
| flow_signal | TEXT | accumulation / distribution |

### sentiment_data  (collector → engine)
| Column | Type | Meaning |
|---|---|---|
| id | INTEGER PK | row id |
| ts | INTEGER | observation time |
| symbol | TEXT | asset |
| sentiment_score | REAL | −1 bearish … +1 bullish |
| credibility | REAL | source-weighted 0–1 |
| novelty | REAL | new vs recycled 0–1 |
| mention_count | INTEGER | chatter volume in window |
| source | TEXT | reddit / telegram / rss |

### signals  (engine → learning loop)
| Column | Type | Meaning |
|---|---|---|
| id | INTEGER PK | row id |
| ts | INTEGER | when evaluated |
| symbol | TEXT | asset |
| market_sub | REAL | market sub-score 0–100 |
| onchain_sub | REAL | on-chain sub-score 0–100 |
| sentiment_sub | REAL | sentiment sub-score 0–100 |
| composite | REAL | weighted total 0–100 |
| direction | TEXT | long / none (v1 long-only) |
| gate_passed | INTEGER | 0 / 1 |
| reason | TEXT | human-readable explanation |

### trades  (engine → learning loop) — the journal
| Column | Type | Meaning |
|---|---|---|
| id | INTEGER PK | row id |
| signal_id | INTEGER FK | → signals.id |
| symbol | TEXT | asset |
| direction | TEXT | long (v1 long-only) |
| mode | TEXT | dry / live |
| entry_ts | INTEGER | entry time |
| entry_price | REAL | fill in |
| size | REAL | position size |
| stop_loss | REAL | SL set at entry |
| take_profit | REAL | TP set at entry |
| exit_ts | INTEGER | exit time (null while open) |
| exit_price | REAL | fill out |
| exit_reason | TEXT | which exit fired |
| pnl | REAL | realized P&L after fees |
| pnl_pct | REAL | return % |
| status | TEXT | open / closed |
| win | INTEGER | 1 if pnl > 0 (set at close) |

---

## 7. Data & seed strategy

Need historical (bulk, to backtest) + live (streaming, to run). Both free. Seed history
first so the whole loop can be built and tested offline before any live feed.

**Market:**
- Historical: **Binance Data Vision** (`data.binance.vision`) — official dumps, no key, no
  rate limit, CSV ZIPs going back years. Best seed. Also CryptoDataDownload (free CSVs since
  2017). Or pull via CCXT / `historical-binance` lib.
- Live: Binance/Bybit WebSocket (free, no key for public market data).

**On-chain:** DefiLlama (free, no key), DexScreener, Etherscan free tier, Dune free tier
(custom SQL), blockchain.com charts for long history.

**Sentiment:**
- Live: Reddit (PRAW), Telegram (Telethon), crypto news RSS — all free.
- Historical aggregate: Fear & Greed Index (`alternative.me`) — free historical endpoint.
- Bootstrap: Kaggle labeled crypto tweet/Reddit/news datasets for classifier development.

**Validate on ingest:** free data has gaps, timestamp misalignment, bad candles (esp. old
altcoins). Check for missing intervals/outliers when loading or backtests will be fictional.

Because everything routes through the DB, **historical and live data look identical to the
engine** — develop against history, then flip to live with no code change.

---

## 8. Open source: public framework, private edge

Implements `SPEC-REQUIREMENTS.md` FR-OS-1/2/3 (the *why* and acceptance tests live there).
Concrete split:

**PUBLIC (committed):** all of `collectors/`, `core/` (scoring & risk *structure*),
`data/schema.sql` + `db.py`, `execution/`, `exchange/`, `learning/` logic,
`config.example.yaml`, the three SDD docs, README, LICENSE, tests.

**PRIVATE (gitignored, NEVER versioned):** `config.yaml` (tuned weights/thresholds),
`secrets.env` (API keys), `storage.db` (journal & history), `models/` (trained ML),
`postmortem_reports/`, logs, downloaded data dumps.

**`.gitignore` must be committed FIRST** (a secret committed once lives in history forever; if
leaked, rotate the key immediately):

```gitignore
# PRIVATE: the edge
config.yaml
config/config.yaml
secrets.env
config/secrets.env
*.env
# PRIVATE: data, journal, results
storage.db
*.db
*.sqlite
/models/
/postmortem_reports/
/logs/
*.log
# PRIVATE: downloaded data
/data/raw/
*.csv
# standard python
__pycache__/
*.pyc
.venv/
venv/
.env
```

License: MIT (friction-free, portfolio-friendly) or GPL-3.0 (forces derivatives open, like
Freqtrade). MIT recommended for this project.

---

## 9. Configuration surface

Everything tunable lives in `config/config.yaml` (gitignored). `config.example.yaml` is the
committed template with safe defaults.

```yaml
mode:
  dry_run: true            # the single most important switch
  position_model: long_only # v1: long-or-flat, spot only (no shorting)

scoring:
  weights:
    market: 0.40
    onchain: 0.25
    sentiment: 0.35
  threshold: 72            # composite must clear this
  require_agreement: true  # all sources must agree LONG to fire

risk:
  kelly_fraction: 0.25     # quarter-Kelly
  max_open_positions: 3
  max_drawdown_pause: 0.15 # pause new entries at -15%
  volatility_filter: true
  min_edge_over_fees: 1.5  # require expected move >= this * round-trip fee

fees:
  maker_pct: 0.0010        # 0.10% — limit/maker orders (set to your exchange tier)
  taker_pct: 0.0010        # 0.10% — market/taker orders
  prefer_maker: true       # enter/exit with limit orders when possible
  # round-trip cost ≈ 2 × (maker or taker) — deducted on every fill, dry & live

exits:
  reward_risk_ratio: 2.0
  use_trailing_stop: true
  max_hold_hours: 48       # HARD CAP — always active, no indefinite holds

evaluation:
  min_trades_for_verdict: 100   # below this, metrics are not a verdict (30 = weak read)
  backtest_winrate_tol_pp: 5    # dry-run vs backtest: win rate within 5 percentage points
  backtest_netresult_tol: 0.20  # ...and net result same sign, within 20%

universe:
  pairs: ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
  timeframe: "1h"
```

---

## 10. Monitoring (no custom UI initially)

Three built-in "interfaces" cover the whole early build:
1. **Telegram** notifications = live feed of signals & trades.
2. **The SQLite file** = read-only UI (open in DB Browser for SQLite to inspect every row).
3. **Weekly postmortem summary** = performance report.

Defer any custom dashboard until tuning from the journal becomes the bottleneck (~after a
full slice works in dry-run). Then add a **read-only Streamlit dashboard** that points at
`storage.db` (equity curve, open positions, recent signals, per-source attribution). Because
everything routes through the DB, the UI is just another reader — zero cost to defer.

---

## 11. Tech stack

- **Language:** Python 3.11+
- **DB:** SQLite (`sqlite3` stdlib; or SQLAlchemy if preferred)
- **Exchange/data:** CCXT, Binance/Bybit WebSocket
- **Indicators:** pandas, pandas-ta (pure-Python, no TA-Lib C build)
- **On-chain:** DefiLlama / Etherscan / Dune REST APIs
- **Sentiment:** PRAW (Reddit), Telethon (Telegram), feedparser (RSS); Ollama for local LLM
  classification, or rule-based
- **Notifications:** python-telegram-bot
- **Config:** PyYAML + python-dotenv
- **(Later) ML:** LightGBM / XGBoost, or FreqAI if going hybrid
- **(Later) UI:** Streamlit
- **Infra:** runs on own machine or free Oracle Cloud VPS; processes under systemd/tmux

---

## 12. Critical reminders for any code written against this spec

- **v1 is long-or-flat, spot only** — never open a short; bearish consensus = exit/stay flat.
- Only ever act on **closed candles**.
- **Time exit is a hard cap** (`max_hold_hours`, default 48h) — no indefinite holds.
- Place **SL/TP atomically with entry**.
- **Deduct fees on every fill (dry & live); store `pnl` net of fees; prefer maker/limit orders.**
- Respect the **dry_run flag** everywhere execution touches the exchange.
- Collectors **write only**, never trade.
- All cross-component communication goes **through the DB**.
- Never commit `secrets.env`, `config.yaml`, or `storage.db`.
- **Don't judge performance below 100 closed trades**; backtest and dry-run must agree within
  tolerance before trusting either.
- This is **educational software** — include disclaimers, default to dry-run, never imply
  guaranteed profit.
