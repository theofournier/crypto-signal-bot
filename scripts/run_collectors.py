"""Launch the live collectors (BUILD_PLAN Phase 3).

In Phase 3 there is one collector: the market collector. It runs one independent
loop per configured pair (FR-DC-4 — a failure on one pair never stalls another),
each appending closed candles to ``market_data``. On-chain and sentiment
collectors arrive in Phase 9 and slot in here the same way.

Usage:
    python3 scripts/run_collectors.py                 # pairs/timeframe from config
    python3 scripts/run_collectors.py --pair BTC/USDT --timeframe 1h
    python3 scripts/run_collectors.py --once          # one cycle then exit (smoke test)

Reads the asset universe from config/config.yaml, falling back to the committed
config/config.example.yaml. Requires ccxt for live exchange access.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

# Allow running as a plain script (python3 scripts/run_collectors.py).
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from collectors.market_collector import MarketCollector  # noqa: E402
from data import db  # noqa: E402

log = logging.getLogger("run_collectors")


def load_universe() -> dict:
    """Read ``universe`` from config.yaml, falling back to config.example.yaml.

    Keeps the safe public template usable out of the box (FR-CF-2) while letting
    the operator's private config.yaml override it without code changes (FR-CF-1).
    """
    import yaml  # local import: only the runner needs PyYAML

    cfg_dir = ROOT / "config"
    path = cfg_dir / "config.yaml"
    if not path.exists():
        path = cfg_dir / "config.example.yaml"
        log.warning("config.yaml not found; using %s defaults", path.name)
    cfg = yaml.safe_load(path.read_text()) or {}
    return cfg.get("universe", {})


def main() -> int:
    parser = argparse.ArgumentParser(description="Run live market collectors")
    parser.add_argument("--pair", action="append", help="override pair(s); repeatable")
    parser.add_argument("--timeframe", help="override timeframe (e.g. 1h)")
    parser.add_argument("--exchange", help="override exchange (e.g. binance)")
    parser.add_argument("--once", action="store_true", help="run one cycle per pair then exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    universe = load_universe()
    pairs = args.pair or universe.get("pairs", ["BTC/USDT"])
    timeframe = args.timeframe or universe.get("timeframe", "1h")
    exchange_name = args.exchange or universe.get("exchange", "binance")

    log.info("collectors: %s @ %s on %s", pairs, timeframe, exchange_name)

    if args.once:
        # Single-threaded smoke test: one connection in this thread is fine.
        conn = db.connect()
        try:
            for p in pairs:
                MarketCollector(
                    conn, symbol=p, timeframe=timeframe, exchange_name=exchange_name
                ).run_once()
        finally:
            conn.close()
        return 0

    _run_forever(pairs, timeframe, exchange_name)
    return 0


def _worker(symbol: str, timeframe: str, exchange_name: str, stop: threading.Event) -> None:
    """Run one pair's collector with its OWN connection + exchange.

    A SQLite connection can only be used in the thread that created it, so each
    collector opens its own here (cheap; SQLite handles multiple connections to
    one file). The ccxt exchange is likewise built per-thread for safety.
    """
    conn = db.connect()
    try:
        MarketCollector(
            conn, symbol=symbol, timeframe=timeframe, exchange_name=exchange_name
        ).run(stop)
    finally:
        conn.close()


def _run_forever(pairs: list[str], timeframe: str, exchange_name: str) -> None:
    """One thread per pair; stop them all cleanly on Ctrl-C/SIGTERM."""
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    threads = [
        threading.Thread(
            target=_worker, args=(p, timeframe, exchange_name, stop),
            name=f"market:{p}", daemon=True,
        )
        for p in pairs
    ]
    for t in threads:
        t.start()
    log.info("running %d collector(s); Ctrl-C to stop", len(threads))
    while not stop.wait(1.0):
        pass
    log.info("shutting down")
    for t in threads:
        t.join(timeout=5.0)


if __name__ == "__main__":
    raise SystemExit(main())
