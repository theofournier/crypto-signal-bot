"""Run the decision engine (BUILD_PLAN Phase 4 — scoring step).

The engine is the single READER of the database: it pulls the latest closed candle
for each configured pair, scores it (``core/normalize`` -> ``core/scoring``), and
writes one ``signals`` row — whether or not the gate fires (FR-SG-4, FR-DP-1). This
is the first step of the engine loop; risk sizing and execution (Phases 5+) bolt on
after this same scoring stage.

Cross-component rule (FR-DP-2): the engine talks to collectors only through the DB.
It never calls a collector and never opens the exchange here.

Dedup: a signal is written once per **new** candle. Re-polling the same closed
candle does not append a duplicate row — the loop only acts when a fresher
``market_data.ts`` than the last stored signal appears for that pair.

Usage:
    python3 scripts/run_engine.py                 # pairs/timeframe from config
    python3 scripts/run_engine.py --pair BTC/USDT --timeframe 1h
    python3 scripts/run_engine.py --once          # one evaluation pass then exit
"""

from __future__ import annotations

import argparse
import logging
import signal as signal_module
import sys
import threading
from pathlib import Path

# Allow running as a plain script (python3 scripts/run_engine.py).
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import scoring  # noqa: E402
from data import db, seed  # noqa: E402

log = logging.getLogger("run_engine")


def latest_market_row(conn, symbol: str, timeframe: str):
    """The most recent closed candle for a pair, or None if none is stored yet."""
    rows = db.query(
        conn,
        "SELECT * FROM market_data WHERE symbol = ? AND timeframe = ? "
        "ORDER BY ts DESC LIMIT 1",
        (symbol, timeframe),
    )
    return rows[0] if rows else None


def last_signal_ts(conn, symbol: str) -> int | None:
    """ts of the newest signal already recorded for a pair (for dedup)."""
    rows = db.query(
        conn, "SELECT MAX(ts) AS ts FROM signals WHERE symbol = ?", (symbol,)
    )
    return rows[0]["ts"] if rows and rows[0]["ts"] is not None else None


def evaluate_pair(conn, symbol: str, timeframe: str, cfg: scoring.ScoringConfig) -> bool:
    """Score a pair's latest candle and persist a signal if it is new.

    Returns True if a signal row was written this pass, False if skipped (no data,
    or the latest candle was already evaluated).
    """
    market_row = latest_market_row(conn, symbol, timeframe)
    if market_row is None:
        log.warning("%s: no market_data yet — skipping", symbol)
        return False

    if market_row["ts"] == last_signal_ts(conn, symbol):
        log.debug("%s: candle %s already evaluated — skipping", symbol, market_row["ts"])
        return False

    evaluation = scoring.evaluate(market_row, cfg)
    db.insert(conn, "signals", evaluation.as_row())
    log.info(
        "%s @ %s: %s | %s",
        symbol,
        market_row["ts"],
        "FIRE" if evaluation.gate_passed else "hold",
        evaluation.reason,
    )
    return True


def run_pass(conn, pairs: list[str], timeframe: str, cfg: scoring.ScoringConfig) -> int:
    """One evaluation pass over all pairs; returns how many signals were written."""
    return sum(evaluate_pair(conn, p, timeframe, cfg) for p in pairs)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the scoring engine")
    parser.add_argument("--pair", action="append", help="override pair(s); repeatable")
    parser.add_argument("--timeframe", help="override timeframe (e.g. 1h)")
    parser.add_argument("--once", action="store_true", help="one evaluation pass then exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    cfg = scoring.load_config()
    universe = cfg.get("universe", {}) or {}
    pairs = args.pair or universe.get("pairs", ["BTC/USDT"])
    timeframe = args.timeframe or universe.get("timeframe", "1h")
    scoring_cfg = scoring.scoring_config(cfg)

    log.info(
        "engine: %s @ %s | weights=%s threshold=%s require_agreement=%s",
        pairs, timeframe, scoring_cfg.weights, scoring_cfg.threshold,
        scoring_cfg.require_agreement,
    )

    conn = db.connect()
    try:
        if args.once:
            run_pass(conn, pairs, timeframe, scoring_cfg)
            return 0
        _run_forever(conn, pairs, timeframe, scoring_cfg)
    finally:
        conn.close()
    return 0


def _run_forever(conn, pairs: list[str], timeframe: str, cfg: scoring.ScoringConfig) -> None:
    """Re-evaluate each pair as new candles close, until Ctrl-C / SIGTERM.

    Polls a little faster than the candle period so a freshly closed candle is
    scored promptly; dedup ensures no duplicate signal per candle.
    """
    stop = threading.Event()
    signal_module.signal(signal_module.SIGINT, lambda *_: stop.set())
    signal_module.signal(signal_module.SIGTERM, lambda *_: stop.set())

    interval = max(seed.timeframe_seconds(timeframe) // 3, 5)
    log.info("evaluating every %ss; Ctrl-C to stop", interval)
    while not stop.is_set():
        try:
            run_pass(conn, pairs, timeframe, cfg)
        except Exception:  # noqa: BLE001 — one bad pass must not kill the engine
            log.exception("evaluation pass failed; continuing")
        stop.wait(interval)
    log.info("shutting down")


if __name__ == "__main__":
    raise SystemExit(main())
