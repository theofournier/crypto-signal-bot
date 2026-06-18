"""Phase 3 tests for the live market collector (collectors/market_collector.py).

Network-free: a FakeExchange returns canned CCXT-format OHLCV (always with a
still-forming last candle, exactly like a real exchange). Tests prove the
non-negotiables: closed-candles-only (no repainting), feature parity with the
seed, idempotent appends, source-isolation in the base loop, and that the
collector only ever writes rows (never trades).

Run with: pytest tests/test_market_collector.py
"""

from __future__ import annotations

import pytest

from collectors.base_collector import BaseCollector
from collectors.market_collector import MarketCollector
from data import db, seed

HOUR_MS = 3_600_000
JAN_2024_OPEN_MS = 1_704_067_200_000  # 2024-01-01 00:00:00 UTC


class FakeExchange:
    """Minimal stand-in for a CCXT exchange.

    ``ohlcv`` rows are [open_ms, o, h, l, c, v]; the LAST row is treated by the
    collector as the still-forming candle (its close lies in the future).
    """

    def __init__(self, ohlcv):
        self.ohlcv = ohlcv
        self.calls = 0

    def fetch_ohlcv(self, symbol, timeframe, limit=None):
        self.calls += 1
        return [list(row) for row in self.ohlcv[-limit:]] if limit else self.ohlcv


def make_ohlcv(count: int, start_ms: int = JAN_2024_OPEN_MS, base: float = 100.0):
    """Rising hourly OHLCV rows in CCXT format (open time in ms)."""
    rows = []
    for i in range(count):
        price = base + i
        rows.append([start_ms + i * HOUR_MS, price, price + 2, price - 2, price + 1, 10.0 + i])
    return rows


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "storage.db")
    yield connection
    connection.close()


def _collector(conn, ohlcv, now_ms):
    """A collector whose 'now' sits at ``now_ms`` so the last row is still forming."""
    return MarketCollector(
        conn,
        symbol="BTC/USDT",
        timeframe="1h",
        exchange=FakeExchange(ohlcv),
        now_fn=lambda: now_ms / 1000,
    )


# ── closed candles only: the repainting guard ───────────────────
def test_never_writes_the_forming_candle(conn):
    ohlcv = make_ohlcv(30)
    # "now" is partway through the last candle's hour → that candle is forming.
    last_open_ms = ohlcv[-1][0]
    mc = _collector(conn, ohlcv, now_ms=last_open_ms + HOUR_MS // 2)
    mc.run_once()

    stored = db.query_all(conn, "market_data")
    # 30 fetched, 1 still forming → 29 closed candles written.
    assert len(stored) == 29
    forming_close_ts = last_open_ms // 1000 + 3600
    assert all(r["ts"] != forming_close_ts for r in stored)  # forming candle absent
    assert max(r["ts"] for r in stored) == last_open_ms // 1000  # newest = prev close


def test_closed_candle_appears_once_its_period_elapses(conn):
    ohlcv = make_ohlcv(30)
    last_open_ms = ohlcv[-1][0]
    # Now the last hour has fully elapsed → the previously-forming candle is closed.
    mc = _collector(conn, ohlcv, now_ms=last_open_ms + HOUR_MS)
    mc.run_once()
    assert len(db.query_all(conn, "market_data")) == 30


# ── feature parity with the seed ─────────────────────────────────
def test_features_match_the_seed_exactly(conn):
    ohlcv = make_ohlcv(60)
    mc = _collector(conn, ohlcv, now_ms=ohlcv[-1][0] + HOUR_MS)
    mc.run_once()

    # Recompute the same candles through the seed pipeline directly.
    candles = [mc._to_candle(r) for r in ohlcv]
    expected = seed.build_rows(*[candles, "BTC/USDT", "1h"])

    stored = {r["ts"]: r for r in db.query_all(conn, "market_data")}
    for row in expected:
        got = stored[row["ts"]]
        for col in ("close", "rsi", "bb_width", "volume_ratio", "vwap_distance"):
            assert got[col] == pytest.approx(row[col]) if row[col] is not None else got[col] is None
    # bid_ask_imbalance is order-book data, absent from OHLCV → NULL (as in the seed).
    assert all(r["bid_ask_imbalance"] is None for r in stored.values())


# ── idempotent appends ───────────────────────────────────────────
def test_rerun_appends_nothing(conn):
    ohlcv = make_ohlcv(40)
    mc = _collector(conn, ohlcv, now_ms=ohlcv[-1][0] + HOUR_MS)
    assert mc.run_once() == 40
    assert mc.run_once() == 0  # same data, nothing new
    assert len(db.query_all(conn, "market_data")) == 40


def test_only_new_candles_are_appended(conn):
    first = make_ohlcv(30)
    mc = _collector(conn, first, now_ms=first[-1][0] + HOUR_MS)
    assert mc.run_once() == 30

    # Next poll returns the same window extended by 5 fresh candles.
    extended = make_ohlcv(35)
    mc.exchange = FakeExchange(extended)
    mc._now = lambda: (extended[-1][0] + HOUR_MS) / 1000
    assert mc.run_once() == 5
    assert len(db.query_all(conn, "market_data")) == 35


# ── base-loop behavior (FR-DC-4 / FR-DC-5) ───────────────────────
def test_run_loop_isolates_a_failing_cycle(conn):
    """A raised fetch error is logged and swallowed — the loop survives."""

    class Boom(BaseCollector):
        table = "market_data"

        def fetch(self):
            raise RuntimeError("source down")

        def normalize(self, raw):  # pragma: no cover - never reached
            return []

    import threading

    c = Boom(conn, interval=0)
    stop = threading.Event()
    c.run_once = _count_then_stop(c.run_once, stop, limit=3)
    c.run(stop)  # returns instead of crashing → failure was isolated


def _count_then_stop(fn, stop, limit):
    calls = {"n": 0}

    def wrapped():
        calls["n"] += 1
        if calls["n"] >= limit:
            stop.set()
        return fn()

    return wrapped


def test_collector_has_no_trading_api(conn):
    """FR-DC-5: a collector observes only — it exposes no order/trade methods."""
    mc = _collector(conn, make_ohlcv(5), now_ms=JAN_2024_OPEN_MS + 100 * HOUR_MS)
    for forbidden in ("create_order", "open_trade", "buy", "sell", "execute"):
        assert not hasattr(mc, forbidden)
