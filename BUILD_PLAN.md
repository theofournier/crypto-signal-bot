# Build Plan — Crypto Signal Bot

> **How to use this file with Claude Code:**
> - Keep `PLAN.md` open in the repo so Claude Code has full architecture context.
> - Work **one phase at a time, top to bottom.** Do not skip ahead.
> - At the end of each phase, confirm the "Done when" criteria before moving on.
> - Tick the checkboxes as you go (`- [ ]` → `- [x]`).
> - The golden rule: **every phase must run in dry-run / offline before the next is added.**
> - A working simple slice beats a perfect half-built system. Resist scope creep.

**Prompt to start any phase with Claude Code:**
> "Read PLAN.md, SPEC-REQUIREMENTS.md and BUILD_PLAN.md. Implement only the tasks in Phase N,
> following the spec exactly. Don't build ahead. Explain what you wrote when done."

---

## Phase 0 — Repo safety & skeleton

**Goal:** a public repo that's safe from the very first commit; secrets & edge can never leak.

- [ ] `git init` and create the GitHub repo (public).
- [ ] Create `.gitignore` FIRST (copy from PLAN §8) and commit it before anything else.
- [ ] Add `LICENSE` (MIT recommended).
- [ ] Add `README.md` with: one-line description, **educational/not-financial-advice
      disclaimer**, the public/private split note, and "always dry-run first" in bold.
- [ ] Create the empty folder structure from PLAN §4 (with `__init__.py` where needed).
- [ ] Add `requirements.txt` (start minimal: `ccxt`, `pandas`, `pandas-ta`, `pyyaml`,
      `python-dotenv`).
- [ ] Add `config/config.example.yaml` (from PLAN §9) and `config/secrets.example.env`.
- [ ] Verify `git status` shows NO `config.yaml`, `secrets.env`, or `*.db`.

**Done when:** repo is public, first commit contains `.gitignore`, and a `git status` proves
no private file is tracked.

---

## Phase 1 — Storage & schema

**Goal:** the database exists and you can read/write every table.

- [x] Write `data/schema.sql` — all 5 tables exactly per PLAN §6.
- [x] Write `data/db.py` — connect, init-schema-if-missing, and simple insert/query helpers.
- [x] Add a tiny script or test that creates `storage.db`, inserts one dummy row per table,
      and reads it back.
- [x] Confirm `storage.db` is gitignored.

**Done when:** running the init creates `storage.db` with all 5 tables, and you can
insert/select rows.

---

## Phase 2 — Seed historical market data

**Goal:** months of real `market_data` in the DB so you can build & backtest offline.

- [x] Write `data/seed.py` + `scripts/seed_data.py` to download Binance Data Vision dumps
      (`data.binance.vision`) for your chosen pairs/timeframe.
- [x] Parse the CSVs and load them into `market_data` (compute the derived columns:
      `volume_ratio`, `rsi`, `bb_width`, `vwap_distance`, `bid_ask_imbalance` where available).
- [x] **Validate on ingest:** detect & log missing intervals, duplicate timestamps, and
      obvious outliers. Don't silently load garbage.
- [x] Seed at least a few months for 1 pair (e.g. BTC/USDT, 1h).

**Done when:** `market_data` holds clean, gap-checked historical rows for at least one pair.

---

## Phase 3 — Market collector (live)

**Goal:** live market rows flowing in, on closed candles only.

- [x] Write `collectors/base_collector.py` (the fetch→normalize→write loop from PLAN §5.1).
- [x] Write `collectors/market_collector.py` — subscribe to Binance/Bybit WebSocket via CCXT,
      process **only closed candles**, compute the same derived features as the seed, write
      to `market_data`.
- [x] Confirm it never writes a still-forming candle (repainting check).
- [x] Wire it into `scripts/run_collectors.py`.

**Done when:** running collectors appends new live rows to `market_data` continuously, only
for completed candles, with no decision logic anywhere in the collector.

---

## Phase 4 — Scoring engine (rules)

**Goal:** the brain emits signals from data, transparently.

- [x] Write `core/normalize.py` — turn raw market features into a 0–100 sub-score + direction.
      (On-chain & sentiment sub-scores can return neutral placeholders for now.)
- [x] Write `core/scoring.py` — weighted composite + gate exactly per PLAN §5.3, reading
      weights/threshold from `config.yaml`.
- [x] On each evaluation, write a row to `signals` (including `reason` text) whether or not it
      fires.
- [x] Wire into `scripts/run_engine.py` as the first step of the engine loop.

**Done when:** the engine reads recent `market_data`, scores it, and writes `signals` rows;
you can read the `reason` and understand every decision.

> Note: with only the market source live, "agreement" is trivially the market direction.
> That's fine — the multi-source gate becomes meaningful in Phase 9.

---

## Phase 5 — Risk gate + execution (DRY-RUN ONLY)

**Goal:** signals become simulated trades with protection, no real money.

- [x] Write `exchange/client.py` — CCXT wrapper with the **dry_run switch** (simulate fills +
      log when `config.mode.dry_run == true`). **Deduct the configured fee on every simulated
      fill** (`config.fees`) exactly as a real exchange would.
- [x] Use **limit (maker) orders** for entry where possible to pay the lower fee (PLAN §5.6).
- [x] Write `core/risk.py` — exposure/drawdown check, volatility filter, fractional-Kelly
      sizing (PLAN §5.4). Add the **fee filter**: skip signals whose expected move is below
      `min_edge_over_fees × round-trip fee`. With no journal yet, use a sane default win_rate/RR.
- [x] Write `execution/executor.py` — **atomic** entry + SL/TP, write `trades` row with
      `status=open`, `mode=dry`.
- [x] Confirm `dry_run: true` is set and NO real orders are possible.

**Done when:** a gated signal produces a simulated `open` trade in `trades` with SL/TP set,
fees are deducted on fills, and you've verified no live order path is reachable while dry_run
is true.

---

## Phase 6 — Monitor loop & the five exits

**Goal:** open trades close correctly via whichever exit fires first.

- [ ] Write `execution/monitor.py` — watch open positions each cycle.
- [ ] Implement all five exits (PLAN §5.5): stop-loss, take-profit, trailing stop, signal
      exit (re-run scoring on open positions), time exit.
- [ ] On close: fetch fill, compute `pnl`/`pnl_pct` after fees, set `exit_reason`,
      `status=closed`, `win`.
- [ ] Verify each exit type triggers correctly in dry-run with crafted scenarios.

**Done when:** dry trades open AND close, every closed row has a correct `exit_reason` and
P&L, and all five exit types have been observed firing.

---

## Phase 7 — Journal + manual postmortem

**Goal:** you can judge the *system*, not just individual trades.

- [ ] Write `learning/postmortem.py` — compute win rate, avg win ÷ avg loss, net P&L after
      fees, max drawdown over closed trades.
- [ ] Add per-source attribution: on losing trades, which sub-scores pointed the wrong way?
- [ ] Output a weekly summary (print, file, or Telegram).
- [ ] Write `notifications/telegram.py` to DM yourself signals + the summary (optional but
      recommended).

**Done when:** you get a readable performance summary from the journal and can see which
source is helping vs hurting.

---

## Phase 8 — Full slice in dry-run (the real milestone)

**Goal:** trust the end-to-end loop on one pair before widening anything.

- [ ] Run collectors + engine together on **one pair** in dry-run for **at least one week**.
- [ ] Confirm the whole chain works unattended: data in → signals → dry trades → exits →
      journal → postmortem.
- [ ] Write `learning/backtest.py` — replay historical `market_data` rows through the SAME
      `core/scoring.py` and compare results to the live dry-run. **The backtest must deduct
      fees on every simulated trade** — a fee-free backtest is fiction.
- [ ] Add `tests/` with at least a lookahead/leakage check (no future data in decisions).
- [ ] Read the postmortem. Hand-tune weights/threshold in `config.yaml` and observe the effect.

**Done when:** the system runs hands-off for a week in dry-run, backtest and dry-run results
are consistent, and you trust the loop. **This is the gate before adding complexity.**

---

## Phase 9 — Widen: more pairs + on-chain + sentiment collectors

**Goal:** the real three-source fusion the design is built for.

- [ ] Add more pairs to `config.yaml` universe; confirm performance/latency is OK.
- [ ] Write `collectors/onchain_collector.py` (DefiLlama/Etherscan → `onchain_data`).
- [ ] Write `collectors/sentiment_collector.py` (Reddit/TG/RSS + classifier → `sentiment_data`).
      Seed historical sentiment from Fear & Greed + a Kaggle dataset for the classifier.
- [ ] Make `core/normalize.py` produce real on-chain & sentiment sub-scores.
- [ ] Now the gate's "all three agree" requirement is fully active — re-validate in dry-run.

**Done when:** all three collectors feed the engine, the multi-source gate is live, and the
widened system has been re-validated in dry-run.

---

## Phase 10 — (Later) ML scoring

**Goal:** let the data find patterns you didn't hand-code. ONLY after hundreds of trades.

- [ ] Confirm the journal has enough closed trades to train on (hundreds, not dozens).
- [ ] Add an optional ML scorer (LightGBM/XGBoost) trained on the journal, or go hybrid with
      FreqAI. Keep it behind a config flag; keep the rules engine as the fallback/baseline.
- [ ] Trained models live in `models/` — **gitignored** (private edge).
- [ ] Compare ML vs rules in backtest + dry-run before trusting it.

**Done when:** the ML scorer measurably beats the rules baseline in dry-run, or you've learned
it doesn't (also valuable).

---

## Phase 11 — (Last) go live, tiny

**Goal:** real money, minimal size, only after everything above checks out.

- [ ] Backtest and dry-run results agree and are net-positive over a meaningful sample.
- [ ] Create read/trade-only exchange API keys in `secrets.env` (never withdrawal perms).
- [ ] Set the **smallest viable position size**.
- [ ] Flip `dry_run: false`. Watch closely. Keep the kill-switch handy.
- [ ] Scale size up only gradually, and only if live results track dry-run.

**Done when:** live trading runs at tiny size with behavior matching dry-run. (Optional next:
add the read-only Streamlit dashboard from PLAN §10.)

---

## Standing reminders (apply to every phase)

- **Dry-run first, always.** `dry_run: true` until Phase 11.
- **Closed candles only.** Never decide on a forming candle.
- **SL/TP atomic with entry.** Never enter unprotected.
- **Fees on every fill.** Deduct fees in dry-run, backtest, and live; store P&L net of fees; prefer maker/limit orders. A fee-free backtest lies.
- **Collectors write only.** No trading logic in collectors.
- **Everything through the DB.** No component calls another directly.
- **Never commit** `secrets.env`, `config.yaml`, or `storage.db`. If a key leaks, rotate it.
- **Educational project.** Disclaimers stay; no profit is promised; risk only what you can
  afford to lose.
