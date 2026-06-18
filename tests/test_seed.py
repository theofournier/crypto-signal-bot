"""Phase 2 tests for the historical seed (data/seed.py).

Network-free: every test feeds synthetic Binance-format CSV bytes through the
real parse -> validate -> feature -> load pipeline. Verifies the two real-world
Binance quirks (no header, ms vs us timestamps), ingest validation (duplicates,
gaps, bad candles), derived features, and an idempotent load into market_data.

Run with: pytest tests/test_seed.py
"""

from __future__ import annotations

import pytest

from data import db, seed


# ── synthetic kline CSV builder ─────────────────────────────────
def make_csv(start_open_ms: int, count: int, step_ms: int = 3_600_000,
             unit: str = "ms", base_price: float = 100.0) -> bytes:
    """Build a clean kline CSV (no header), like a Binance monthly dump."""
    scale = {"ms": 1, "us": 1000, "s": 0.001}[unit]  # ms -> chosen unit
    step_native = step_ms * scale
    lines = []
    for i in range(count):
        # Compute in native units so close is the last tick of the period
        # (real us dumps end in ...999999, not a scaled-up ms value).
        open_t = int((start_open_ms + i * step_ms) * scale)
        close_t = int(open_t + step_native - 1)
        price = base_price + i
        # open, high, low, close, volume
        lines.append(
            f"{open_t},{price},{price + 2},{price - 2},{price + 1},{10.0 + i},"
            f"{close_t},0,0,0,0,0"
        )
    return "\n".join(lines).encode()


JAN_2024_OPEN_MS = 1_704_067_200_000  # 2024-01-01 00:00:00 UTC


# ── timeframe + timestamp normalization ─────────────────────────
def test_timeframe_seconds():
    assert seed.timeframe_seconds("1h") == 3600
    assert seed.timeframe_seconds("15m") == 900
    assert seed.timeframe_seconds("1d") == 86400


def test_parse_handles_millisecond_and_microsecond_units():
    ms = seed.parse_csv(make_csv(JAN_2024_OPEN_MS, 1, unit="ms"))[0]
    us = seed.parse_csv(make_csv(JAN_2024_OPEN_MS, 1, unit="us"))[0]
    # Both normalize to the same unix-second grid.
    assert ms.open_ts == us.open_ts == JAN_2024_OPEN_MS // 1000
    # close_ts lands on the clean period boundary (open + 1h), not ...:59:59.
    assert ms.close_ts == ms.open_ts + 3600
    assert us.close_ts == us.open_ts + 3600


def test_parse_skips_optional_header_row():
    body = make_csv(JAN_2024_OPEN_MS, 2)
    with_header = b"open_time,open,high,low,close,volume,close_time,a,b,c,d,e\n" + body
    assert len(seed.parse_csv(with_header)) == 2


def test_file_symbol_strips_slash():
    assert seed.file_symbol("BTC/USDT") == "BTCUSDT"


# ── completed_months never includes the current (forming) month ─
def test_completed_months_excludes_current():
    from datetime import datetime, timezone
    now = datetime(2026, 6, 18, tzinfo=timezone.utc)
    months = seed.completed_months(3, today=now)
    assert months == [(2026, 3), (2026, 4), (2026, 5)]  # oldest first, no June


# ── validation on ingest ────────────────────────────────────────
def test_validate_drops_duplicates():
    candles = seed.parse_csv(make_csv(JAN_2024_OPEN_MS, 3))
    candles.append(candles[0])  # duplicate open_ts
    kept, report = seed.validate(candles, "1h")
    assert report.duplicates_dropped == 1
    assert len(kept) == 3


def test_validate_reports_missing_intervals():
    # Two blocks with a 2-candle hole between them.
    block_a = seed.parse_csv(make_csv(JAN_2024_OPEN_MS, 2))
    block_b = seed.parse_csv(make_csv(JAN_2024_OPEN_MS + 4 * 3_600_000, 2))
    kept, report = seed.validate(block_a + block_b, "1h")
    assert report.missing_intervals == 2
    assert report.gap_examples  # human-readable gap logged


def test_validate_drops_structurally_invalid_candle():
    candles = seed.parse_csv(make_csv(JAN_2024_OPEN_MS, 3))
    candles[1].high = candles[1].low - 1  # high below low → invalid
    kept, report = seed.validate(candles, "1h")
    assert report.invalid_dropped == 1
    assert len(kept) == 2


# ── derived features ────────────────────────────────────────────
def test_build_rows_computes_features_where_enough_history():
    candles = seed.parse_csv(make_csv(JAN_2024_OPEN_MS, 60))
    rows = seed.build_rows(candles, "BTC/USDT", "1h")
    last = rows[-1]
    # With 60 monotonically rising closes, RSI is defined and high.
    assert last["rsi"] is not None and last["rsi"] > 90
    assert last["bb_width"] is not None and last["bb_width"] > 0
    assert last["vwap_distance"] is not None
    # Order-book imbalance is not in OHLCV dumps → always NULL from the seed.
    assert last["bid_ask_imbalance"] is None
    # ts is the candle CLOSE time (FR-DC-6: closed candles only).
    assert last["ts"] == candles[-1].close_ts


def test_features_are_none_until_enough_history():
    rows = seed.build_rows(seed.parse_csv(make_csv(JAN_2024_OPEN_MS, 60)), "BTC/USDT", "1h")
    assert rows[0]["rsi"] is None       # needs 14 candles
    assert rows[0]["bb_width"] is None  # needs 20 candles


# ── end-to-end load (idempotent) ────────────────────────────────
@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "storage.db")
    yield connection
    connection.close()


def _load(conn, candles, symbol="BTC/USDT", timeframe="1h"):
    kept, _ = seed.validate(candles, timeframe)
    rows = seed.build_rows(kept, symbol, timeframe)
    rows = seed._drop_existing(conn, symbol, timeframe, rows)
    return db.insert_many(conn, "market_data", rows)


def test_load_inserts_clean_rows(conn):
    candles = seed.parse_csv(make_csv(JAN_2024_OPEN_MS, 30))
    inserted = _load(conn, candles)
    assert inserted == 30
    stored = db.query_all(conn, "market_data")
    assert len(stored) == 30
    assert stored[0]["symbol"] == "BTC/USDT"
    assert all(r["ts"] is not None for r in stored)


def test_reload_is_idempotent(conn):
    candles = seed.parse_csv(make_csv(JAN_2024_OPEN_MS, 30))
    assert _load(conn, candles) == 30
    assert _load(conn, candles) == 0  # second run inserts nothing
    assert len(db.query_all(conn, "market_data")) == 30
