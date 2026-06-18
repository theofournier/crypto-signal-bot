"""Initialize the database and prove the round-trip works (Phase 1).

Usage:
    python3 scripts/init_db.py            # create/verify storage.db at repo root
    python3 scripts/init_db.py --selftest # also insert+read one dummy row per table

Creates storage.db with all five tables (PLAN.md §6). storage.db is gitignored.
Uses only the standard library, so it runs without installing anything.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

# Allow running as a plain script (python3 scripts/init_db.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data import db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize storage.db")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="insert and read back one dummy row per table in a temp DB",
    )
    args = parser.parse_args()

    conn = db.connect(db.DEFAULT_DB_PATH)
    tables = sorted(
        r["name"]
        for r in db.query(conn, "SELECT name FROM sqlite_master WHERE type='table'")
    )
    conn.close()
    print(f"storage.db ready at {db.DEFAULT_DB_PATH}")
    print(f"tables: {tables}")
    missing = set(db.TABLES) - set(tables)
    if missing:
        print(f"ERROR: missing tables {sorted(missing)}")
        return 1

    if args.selftest:
        _selftest()
    return 0


def _selftest() -> None:
    """Insert one dummy row per table into a throwaway DB and read it back."""
    tmp = Path(tempfile.mkdtemp()) / "storage.db"
    conn = db.connect(tmp)
    rows = {
        "market_data": {"ts": 1, "symbol": "BTC/USDT", "timeframe": "1h", "close": 40250.0},
        "onchain_data": {"ts": 1, "symbol": "BTC/USDT", "net_flow": 500_000.0},
        "sentiment_data": {"ts": 1, "symbol": "BTC/USDT", "sentiment_score": 0.42},
        "signals": {"ts": 1, "symbol": "BTC/USDT", "composite": 73.5,
                    "direction": "long", "gate_passed": 1, "reason": "selftest"},
    }
    for table, row in rows.items():
        new_id = db.insert(conn, table, row)
        back = db.query_all(conn, table)
        assert len(back) == 1 and back[0]["id"] == new_id, f"{table} round-trip failed"
    # trades references a real signal (foreign key is enforced).
    signal_id = db.query_all(conn, "signals")[0]["id"]
    db.insert(conn, "trades", {"signal_id": signal_id, "symbol": "BTC/USDT",
                               "direction": "long", "mode": "dry", "status": "open"})
    assert db.query_all(conn, "trades")[0]["signal_id"] == signal_id
    conn.close()
    print("selftest: inserted + read back one row in every table OK")


if __name__ == "__main__":
    raise SystemExit(main())
