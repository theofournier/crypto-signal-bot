"""One-off historical seed (BUILD_PLAN Phase 2; widened in Phase 9).

Downloads Binance Data Vision monthly OHLCV dumps and loads clean, gap-checked
candles into ``market_data`` so later phases can backtest offline (PLAN §7).
Standard library only — runs without installing anything.

By default it seeds the **entire configured universe** (``universe.pairs`` /
``universe.timeframe`` from config.yaml, falling back to config.example.yaml), so
widening the universe in Phase 9 is a one-command catch-up. ``--pair`` overrides
the universe to seed just the pairs you name.

Usage:
    python3 scripts/seed_data.py                                  # all config pairs
    python3 scripts/seed_data.py --pair ETH/USDT --pair SOL/USDT  # only these
    python3 scripts/seed_data.py --pair BTC/USDT --months 6 -v    # verbose (gaps, etc.)

Defaults mirror config: pairs/timeframe from ``universe``, months from
``data.history_days`` (rounded up to whole months). storage.db and downloaded
dumps are gitignored — nothing private is committed.
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
from pathlib import Path

# Allow running as a plain script (python3 scripts/seed_data.py).
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import scoring  # noqa: E402 — reuse the one config loader (FR-CF-1/2)
from data import db, seed  # noqa: E402

log = logging.getLogger("seed_data")


def _months_from_history_days(cfg: dict, default: int = 6) -> int:
    """Whole calendar months covering ``data.history_days`` (config-driven)."""
    days = ((cfg.get("data", {}) or {}).get("history_days"))
    if not days:
        return default
    return max(1, math.ceil(days / 30))


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed historical market_data")
    parser.add_argument(
        "--pair", action="append",
        help="override the universe; repeatable (e.g. --pair ETH/USDT --pair SOL/USDT)",
    )
    parser.add_argument("--timeframe", help="override timeframe (default: config universe)")
    parser.add_argument(
        "--months", type=int,
        help="completed calendar months to seed (default: from data.history_days)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log gaps/warnings")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    # Always show seed progress, even without -v.
    logging.getLogger("seed").setLevel(logging.INFO)
    log.setLevel(logging.INFO)

    cfg = scoring.load_config()
    universe = cfg.get("universe", {}) or {}
    pairs = args.pair or universe.get("pairs", ["BTC/USDT"])
    timeframe = args.timeframe or universe.get("timeframe", "1h")
    months = args.months if args.months is not None else _months_from_history_days(cfg)

    log.info("seeding %d pair(s) @ %s, %d month(s): %s", len(pairs), timeframe, months, pairs)

    conn = db.connect()
    seeded_any = False
    try:
        for pair in pairs:
            try:
                inserted = seed.seed(
                    symbol=pair, timeframe=timeframe, months=months, conn=conn
                )
            except Exception:  # noqa: BLE001 — one bad pair must not abort the rest
                log.exception("%s: seed failed — skipping", pair)
                continue
            total = db.query(
                conn,
                "SELECT COUNT(*) AS n FROM market_data WHERE symbol = ? AND timeframe = ?",
                (pair, timeframe),
            )[0]["n"]
            seeded_any = seeded_any or total > 0
            print(f"{pair} {timeframe}: +{inserted} new rows ({total} total)")
    finally:
        conn.close()

    return 0 if seeded_any else 1


if __name__ == "__main__":
    raise SystemExit(main())
