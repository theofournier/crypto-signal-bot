"""One-off historical seed (BUILD_PLAN Phase 2).

Downloads Binance Data Vision monthly OHLCV dumps and loads clean, gap-checked
candles into ``market_data`` so later phases can backtest offline (PLAN §7).
Standard library only — runs without installing anything.

Usage:
    python3 scripts/seed_data.py                                  # BTC/USDT 1h, 6 months
    python3 scripts/seed_data.py --pair ETH/USDT --timeframe 1h --months 4
    python3 scripts/seed_data.py --pair BTC/USDT --months 6 -v    # verbose (gaps, etc.)

Defaults mirror config.example.yaml (universe.pairs[0], timeframe, ~history_days).
storage.db and downloaded dumps are gitignored — nothing private is committed.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow running as a plain script (python3 scripts/seed_data.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data import db, seed  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed historical market_data")
    parser.add_argument("--pair", default="BTC/USDT", help="e.g. BTC/USDT")
    parser.add_argument("--timeframe", default="1h", help="e.g. 1h, 15m, 1d")
    parser.add_argument(
        "--months", type=int, default=6,
        help="number of completed calendar months to seed (default 6 ≈ 180 days)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log gaps/warnings")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    # Always show the seed module's INFO progress, even without -v.
    logging.getLogger("seed").setLevel(logging.INFO)

    conn = db.connect()
    try:
        inserted = seed.seed(
            symbol=args.pair,
            timeframe=args.timeframe,
            months=args.months,
            conn=conn,
        )
        total = db.query(
            conn,
            "SELECT COUNT(*) AS n FROM market_data WHERE symbol = ? AND timeframe = ?",
            (args.pair, args.timeframe),
        )[0]["n"]
    finally:
        conn.close()

    print(f"inserted {inserted} new rows; {args.pair} {args.timeframe} now has {total} rows")
    return 0 if total > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
