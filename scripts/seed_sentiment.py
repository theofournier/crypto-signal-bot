"""One-off historical sentiment seed (BUILD_PLAN Phase 9).

Backfills ``sentiment_data`` with the **Fear & Greed Index** history from
alternative.me (free, no key — PLAN §7) so the engine and backtester have a real
sentiment series to replay offline before the live collector accumulates one. Each
daily index value is mapped to a directional ``sentiment_score`` (the same mapping
the live :class:`FearGreedSource` uses) and written once per configured asset
(the index is market-wide), with ``source = "fear_greed"``.

Idempotent: a ``(ts, symbol, source)`` already present is skipped, so re-running
only adds new days. Standard library only — runs without installing anything.

Usage:
    python3 scripts/seed_sentiment.py                 # all config pairs, full history
    python3 scripts/seed_sentiment.py --pair BTC/USDT # only this asset
    python3 scripts/seed_sentiment.py --days 365 -v   # last year only, verbose

> Note on the classifier: the rule-based default classifier needs **no** training
> data, so no Kaggle download is required to run the system. A Kaggle labeled-text
> dataset is only useful later if you replace it with a learned classifier — drop
> the CSV under data/raw/ (gitignored) and train your own SentimentClassifier.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.request
from pathlib import Path

# Allow running as a plain script (python3 scripts/seed_sentiment.py).
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from collectors.sentiment_collector import (  # noqa: E402
    DEFAULT_FNG_CREDIBILITY,
    FNG_BASE_URL,
    fng_score,
    parse_fng_history,
)
from core import scoring  # noqa: E402 — reuse the one config loader (FR-CF-1/2)
from data import db  # noqa: E402

log = logging.getLogger("seed_sentiment")


def fetch_fng_history(timeout: float = 30.0) -> dict:
    """Download the full Fear & Greed daily history (alternative.me, no key)."""
    url = f"{FNG_BASE_URL}/fng/?limit=0&format=json"
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - fixed https host
        return json.loads(resp.read().decode())


def _existing_ts(conn, symbol: str) -> set[int]:
    """Timestamps already seeded for this asset's fear_greed series (idempotency)."""
    rows = db.query(
        conn,
        "SELECT ts FROM sentiment_data WHERE symbol = ? AND source = 'fear_greed'",
        (symbol,),
    )
    return {int(r["ts"]) for r in rows}


def seed_symbol(conn, symbol: str, history: list[tuple[int, float]], credibility: float) -> int:
    """Write any not-yet-stored Fear & Greed days for ``symbol``; return rows added."""
    existing = _existing_ts(conn, symbol)
    rows = [
        {
            "ts": ts,
            "symbol": symbol,
            "sentiment_score": fng_score(value),
            "credibility": credibility,
            "novelty": None,
            "mention_count": None,
            "source": "fear_greed",
        }
        for ts, value in history
        if ts not in existing
    ]
    return db.insert_many(conn, "sentiment_data", rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed historical sentiment (Fear & Greed)")
    parser.add_argument(
        "--pair", action="append",
        help="override the universe; repeatable (e.g. --pair BTC/USDT --pair ETH/USDT)",
    )
    parser.add_argument("--days", type=int, help="only seed the most recent N days")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    log.setLevel(logging.INFO)

    cfg = scoring.load_config()
    universe = cfg.get("universe", {}) or {}
    pairs = args.pair or universe.get("pairs", ["BTC/USDT"])
    credibility = float((cfg.get("sentiment", {}) or {}).get("fear_greed", {}).get(
        "credibility", DEFAULT_FNG_CREDIBILITY))

    log.info("fetching Fear & Greed history…")
    try:
        history = parse_fng_history(fetch_fng_history())
    except Exception:  # noqa: BLE001 — a network failure is a clean exit, not a crash
        log.exception("could not fetch Fear & Greed history")
        return 1
    if not history:
        log.error("no Fear & Greed history returned")
        return 1

    if args.days:
        cutoff = int(time.time()) - args.days * 86400
        history = [(ts, v) for ts, v in history if ts >= cutoff]
    log.info("seeding %d day(s) across %d pair(s): %s", len(history), len(pairs), pairs)

    conn = db.connect()
    seeded_any = False
    try:
        for pair in pairs:
            added = seed_symbol(conn, pair, history, credibility)
            total = db.query(
                conn,
                "SELECT COUNT(*) AS n FROM sentiment_data WHERE symbol = ? AND source = 'fear_greed'",
                (pair,),
            )[0]["n"]
            seeded_any = seeded_any or total > 0
            print(f"{pair} fear_greed: +{added} new rows ({total} total)")
    finally:
        conn.close()

    return 0 if seeded_any else 1


if __name__ == "__main__":
    raise SystemExit(main())
