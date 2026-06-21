"""Replay history through the SAME engine, fees and all (BUILD_PLAN Phase 8).

A backtest is only worth trusting if it runs the *identical* decision code the live
loop runs. This module does exactly that: it replays stored ``market_data`` rows,
one closed candle at a time, through the very same pipeline as ``scripts/run_engine``

    score (``core/scoring``) → risk gate (``core/risk``) → entry (``execution/executor``)
    → exits (``execution/monitor``)

so a backtest result and a live dry-run result are directly comparable (Phase 8
"done when": backtest and dry-run are consistent). Nothing here re-implements the
strategy — it only drives the real components over historical data.

**Fees on every fill (FR-EX-3, PLAN §5.6).** Entries and exits go through the same
``exchange/client`` dry-run simulator the live loop uses, so the configured maker/
taker fee is deducted on every simulated fill. A fee-free backtest is fiction
(BUILD_PLAN standing reminder) — this one is not.

**Leakage-safe (NFR-TEST, G4).** The replay feeds candles into a scratch DB in
chronological order and acts only on the candle just inserted; every read the engine
makes (latest candle, ATR context, trailing peak ``ts > entry_ts``) therefore sees
*only past-or-present* rows — never a future candle. Time is the market clock (candle
``ts``), injected as the order/entry timestamp, so the time-exit and the entry-candle
leakage guard behave exactly as in dry-run. ``tests/test_backtest.py`` asserts this.

**Isolated from the live journal.** The replay runs against an in-memory scratch DB,
so it never writes ``signals``/``trades`` into ``storage.db``. The source candles are
read from the real DB read-only.

This module reads the DB and reuses the engine components; it places no real order
(dry-run only) and changes no decision logic. Results are summarized with the very
same ``learning/postmortem`` reporting the live journal uses, so the two reports line
up field-for-field.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

# Allow running as a plain script (python3 learning/backtest.py) as well as a module.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import risk, scoring  # noqa: E402
from data import db  # noqa: E402
from exchange.client import ExchangeClient, Fees  # noqa: E402
from execution import executor, monitor  # noqa: E402
from learning import postmortem  # noqa: E402

log = logging.getLogger("backtest")

# How many recent candles to pull as context for the ATR-based stop (mirrors
# run_engine.CONTEXT_CANDLES so the backtest sizes stops exactly as the live loop).
CONTEXT_CANDLES = 60

# Columns we copy from a source market_data row into the scratch DB. ``id`` is
# dropped so the scratch table assigns its own ids.
_MARKET_COLUMNS = (
    "ts", "symbol", "timeframe", "open", "high", "low", "close", "volume",
    "volume_ratio", "bid_ask_imbalance", "bb_width", "rsi", "vwap_distance",
)


@dataclass
class BacktestResult:
    """The outcome of one replay: the sample size, the journal, and the report.

    ``metrics`` and ``attribution`` are computed with ``learning/postmortem`` over
    the simulated trades, so they are the same shape as the live dry-run summary and
    can be compared field-for-field (Phase 8). ``report`` is the formatted string.
    """

    symbol: str
    timeframe: str
    n_candles: int
    n_signals: int
    n_trades: int
    metrics: postmortem.Metrics
    attribution: list
    report: str


def _clock():
    """A tiny mutable market clock: ``get`` reads it, ``set`` advances it.

    The engine components take ``now_fn`` callables (entry timestamp, fill ts). In a
    backtest "now" is the timestamp of the candle being replayed, not wall-clock, so
    we hand them this getter and advance it candle by candle. This is what makes the
    time-exit and the entry-candle leakage guard use market time (FR-SF-3, G4).
    """
    box = {"ts": 0}
    return box


def load_market_rows(
    conn,
    symbol: str,
    timeframe: str,
    since_ts: int | None = None,
    until_ts: int | None = None,
) -> list:
    """Historical closed candles for a pair, oldest first (the replay input).

    Read-only over the source DB. ``since_ts``/``until_ts`` bound the window so a
    backtest can be cut to the same period as a dry-run for a like-for-like compare.
    """
    sql = "SELECT * FROM market_data WHERE symbol = ? AND timeframe = ?"
    params: list = [symbol, timeframe]
    if since_ts is not None:
        sql += " AND ts >= ?"
        params.append(since_ts)
    if until_ts is not None:
        sql += " AND ts <= ?"
        params.append(until_ts)
    sql += " ORDER BY ts"
    return db.query(conn, sql, params)


def _recent_market_rows(conn, symbol: str, timeframe: str, limit: int = CONTEXT_CANDLES):
    """Last ``limit`` candles for a pair, oldest first (ATR/volatility context).

    Mirrors ``run_engine.recent_market_rows`` so the ATR stop distance is computed
    over the same window as the live loop. Reads only the scratch DB, which by
    construction holds candles up to and including the current one — never the future.
    """
    rows = db.query(
        conn,
        "SELECT * FROM market_data WHERE symbol = ? AND timeframe = ? "
        "ORDER BY ts DESC LIMIT ?",
        (symbol, timeframe, limit),
    )
    return list(reversed(rows))


def _market_insert_row(candle) -> dict:
    """Shape a source candle into a scratch ``market_data`` insert (drops ``id``)."""
    return {col: candle[col] for col in _MARKET_COLUMNS}


def run_replay(
    rows,
    cfg: dict,
    symbol: str,
    timeframe: str,
) -> BacktestResult:
    """Replay ``rows`` (chronological candles) through the live engine pipeline.

    Each step inserts the next candle into a scratch DB, scores it, runs the risk
    gate + executor on a fired signal, then runs the monitor over open positions —
    the exact ``score → risk → execute → monitor`` order of ``run_engine.run_pass``.
    Because the scratch DB only ever holds candles up to the current step, every read
    the engine makes is leakage-safe. Returns the metrics/attribution/report over the
    resulting simulated journal.
    """
    scoring_cfg = scoring.scoring_config(cfg)
    exits_cfg = executor.ExitConfig.from_config(cfg)
    bankroll = risk.RiskConfig.from_config(cfg).bankroll

    clock = _clock()
    # A dry-run client whose fill timestamps follow the market clock; fees on every
    # fill come straight from config, exactly as the live loop deducts them.
    client = ExchangeClient(
        fees=Fees.from_config(cfg), dry_run=True, now_fn=lambda: clock["ts"]
    )
    scratch = db.connect(":memory:")
    risk_gate = risk.RiskGate(scratch, risk.RiskConfig.from_config(cfg), mode=client.mode)

    n_signals = 0
    try:
        for candle in rows:
            ts = int(candle["ts"])
            clock["ts"] = ts
            db.insert(scratch, "market_data", _market_insert_row(candle))

            # 1. Score the just-closed candle and persist the signal (fire or not).
            evaluation = scoring.evaluate(candle, scoring_cfg)
            signal_id = db.insert(scratch, "signals", evaluation.as_row())
            n_signals += 1

            # 2. On a fired signal, risk-gate then (atomically) open a dry trade.
            if evaluation.gate_passed:
                executor.try_open_from_signal(
                    scratch,
                    client,
                    risk_gate,
                    exits_cfg,
                    signal_id,
                    evaluation.as_row(),
                    candle,
                    _recent_market_rows(scratch, symbol, timeframe),
                    now_fn=lambda: clock["ts"],
                )

            # 3. Race the five exits over every open position on this candle. Uses
            #    market time, so time-exit and the entry-candle leakage guard match
            #    dry-run exactly.
            monitor.monitor_open_trades(scratch, client, scoring_cfg, exits_cfg, timeframe)

        trade_rows = postmortem.closed_trades_with_signals(scratch, mode=client.mode)
        metrics = postmortem.compute_metrics(trade_rows, bankroll)
        attribution = postmortem.compute_attribution(trade_rows)
        period_label = f"backtest replay of {len(rows)} candle(s)"
        report = postmortem.format_summary(
            metrics, attribution, mode="backtest", period_label=period_label
        )
        return BacktestResult(
            symbol=symbol,
            timeframe=timeframe,
            n_candles=len(rows),
            n_signals=n_signals,
            n_trades=metrics.n_closed,
            metrics=metrics,
            attribution=attribution,
            report=report,
        )
    finally:
        scratch.close()


def backtest(
    conn,
    symbol: str,
    timeframe: str,
    cfg: dict,
    since_ts: int | None = None,
    until_ts: int | None = None,
) -> BacktestResult:
    """Load history for a pair from ``conn`` and replay it (load + run_replay)."""
    rows = load_market_rows(conn, symbol, timeframe, since_ts, until_ts)
    log.info("backtest %s @ %s: %d candle(s)", symbol, timeframe, len(rows))
    return run_replay(rows, cfg, symbol, timeframe)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Backtest the engine over stored historical market_data"
    )
    parser.add_argument("--pair", help="pair to backtest (default: first in config universe)")
    parser.add_argument("--timeframe", help="timeframe (default: from config universe)")
    parser.add_argument("--days", type=int, default=None,
                        help="only candles from the last N days (default: all history)")
    parser.add_argument("--compare", action="store_true",
                        help="also print the live dry-run postmortem for comparison")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    cfg = scoring.load_config()
    universe = cfg.get("universe", {}) or {}
    pairs = universe.get("pairs", ["BTC/USDT"])
    symbol = args.pair or (pairs[0] if pairs else "BTC/USDT")
    timeframe = args.timeframe or universe.get("timeframe", "1h")

    conn = db.connect()
    try:
        since_ts = None
        if args.days is not None:
            import time

            since_ts = int(time.time()) - args.days * 86_400
        result = backtest(conn, symbol, timeframe, cfg, since_ts=since_ts)
        print(result.report)

        if args.compare:
            bankroll = risk.RiskConfig.from_config(cfg).bankroll
            live = postmortem.build_report(conn, mode="dry", days=args.days, bankroll=bankroll)
            print()
            print("─── live dry-run journal (for comparison) ───")
            print(live)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
