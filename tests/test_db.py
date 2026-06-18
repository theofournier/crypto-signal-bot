"""Phase 1 round-trip test: create the DB, insert one dummy row per table, read
it back. Verifies schema.sql + db.py against PLAN.md §6.

Run with: pytest tests/test_db.py
"""

from __future__ import annotations

import sqlite3

import pytest

from data import db

# One representative dummy row per table (a subset of columns is fine — the rest
# default to NULL). Values are arbitrary; this only proves write->read works.
DUMMY_ROWS = {
    "market_data": {
        "ts": 1_700_000_000,
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "open": 40000.0,
        "high": 40500.0,
        "low": 39800.0,
        "close": 40250.0,
        "volume": 1234.5,
        "rsi": 55.2,
    },
    "onchain_data": {
        "ts": 1_700_000_000,
        "symbol": "BTC/USDT",
        "exchange_inflow": 1_000_000.0,
        "exchange_outflow": 1_500_000.0,
        "net_flow": 500_000.0,
        "whale_tx_count": 7,
        "flow_signal": "accumulation",
    },
    "sentiment_data": {
        "ts": 1_700_000_000,
        "symbol": "BTC/USDT",
        "sentiment_score": 0.42,
        "credibility": 0.8,
        "novelty": 0.5,
        "mention_count": 120,
        "source": "reddit",
    },
    "signals": {
        "ts": 1_700_000_000,
        "symbol": "BTC/USDT",
        "market_sub": 80.0,
        "onchain_sub": 70.0,
        "sentiment_sub": 65.0,
        "composite": 73.5,
        "direction": "long",
        "gate_passed": 1,
        "reason": "all sources agree long; composite 73.5 >= 72",
    },
    "trades": {
        "symbol": "BTC/USDT",
        "direction": "long",
        "mode": "dry",
        "entry_ts": 1_700_000_000,
        "entry_price": 40250.0,
        "size": 0.01,
        "stop_loss": 39500.0,
        "take_profit": 41750.0,
        "status": "open",
    },
}


@pytest.fixture()
def conn(tmp_path):
    """A fresh DB in a temp file (keeps the real storage.db untouched)."""
    connection = db.connect(tmp_path / "storage.db")
    yield connection
    connection.close()


def test_connect_creates_all_five_tables(conn):
    names = {
        r["name"]
        for r in db.query(
            conn, "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert set(db.TABLES).issubset(names)


@pytest.mark.parametrize("table", db.TABLES)
def test_insert_and_read_back(conn, table):
    row = DUMMY_ROWS[table]
    new_id = db.insert(conn, table, row)

    fetched = db.query_all(conn, table)
    assert len(fetched) == 1
    assert fetched[0]["id"] == new_id
    # Every value we wrote round-trips unchanged.
    for column, value in row.items():
        assert fetched[0][column] == value


def test_trades_foreign_key_links_to_signals(conn):
    signal_id = db.insert(conn, "signals", DUMMY_ROWS["signals"])
    trade = {**DUMMY_ROWS["trades"], "signal_id": signal_id}
    db.insert(conn, "trades", trade)
    assert db.query_all(conn, "trades")[0]["signal_id"] == signal_id

    # A dangling signal_id must be rejected (foreign keys are enforced).
    bad = {**DUMMY_ROWS["trades"], "signal_id": 999_999}
    with pytest.raises(sqlite3.IntegrityError):
        db.insert(conn, "trades", bad)


def test_unknown_table_is_rejected(conn):
    with pytest.raises(ValueError):
        db.insert(conn, "robots", {"x": 1})
