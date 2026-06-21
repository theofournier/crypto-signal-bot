"""Phase 9 tests for the on-chain collector (collectors/onchain_collector.py).

Network-free: a FakeSource returns canned transfers, so the tests prove the
non-negotiables without hitting Etherscan — directional reduction
(accumulation/distribution), USD conversion via the stored market price, graceful
degradation for uncovered assets / missing price, the pure Etherscan payload
parser, source-isolation in the base loop, and that the collector only ever writes
rows (never trades).

Run with: pytest tests/test_onchain_collector.py
"""

from __future__ import annotations

import threading

import pytest

from collectors.base_collector import BaseCollector
from collectors.onchain_collector import (
    ACCUMULATION,
    DISTRIBUTION,
    NEUTRAL,
    EtherscanSource,
    OnChainCollector,
    OnChainSource,
    Transfer,
    parse_tokentx,
    summarize_flows,
)
from data import db

NOW = 1_704_067_200  # 2024-01-01 00:00:00 UTC (seconds)


class FakeSource(OnChainSource):
    """Returns a fixed transfer list (or None to mean 'asset not covered')."""

    def __init__(self, transfers):
        self._transfers = transfers
        self.calls = 0

    def transfers(self, symbol, base_asset, since_ts, now_ts):
        self.calls += 1
        return self._transfers


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "storage.db")
    yield connection
    connection.close()


def _seed_price(conn, symbol, close, ts=NOW - 3600):
    db.insert(conn, "market_data", {"ts": ts, "symbol": symbol, "timeframe": "1h", "close": close})


def _collector(conn, source, **kw):
    return OnChainCollector(
        conn, symbol="LINK/USDT", source=source, now_fn=lambda: NOW, **kw
    )


# ── directional reduction (pure) ─────────────────────────────────
def test_net_outflow_is_accumulation():
    transfers = [
        Transfer(ts=NOW, amount=100.0, direction="out"),  # leaving exchanges
        Transfer(ts=NOW, amount=10.0, direction="in"),
    ]
    r = summarize_flows(transfers, price_usd=2.0, whale_usd=1e9, deadband=0.05)
    assert r["exchange_outflow"] == pytest.approx(200.0)
    assert r["exchange_inflow"] == pytest.approx(20.0)
    assert r["net_flow"] == pytest.approx(180.0)
    assert r["flow_signal"] == ACCUMULATION


def test_net_inflow_is_distribution():
    transfers = [Transfer(ts=NOW, amount=100.0, direction="in")]
    r = summarize_flows(transfers, price_usd=2.0, whale_usd=1e9, deadband=0.05)
    assert r["net_flow"] < 0
    assert r["flow_signal"] == DISTRIBUTION


def test_balanced_flow_is_neutral_within_deadband():
    transfers = [
        Transfer(ts=NOW, amount=100.0, direction="in"),
        Transfer(ts=NOW, amount=101.0, direction="out"),  # 1% imbalance < 5% deadband
    ]
    r = summarize_flows(transfers, price_usd=1.0, whale_usd=1e9, deadband=0.05)
    assert r["flow_signal"] == NEUTRAL


def test_whales_counted_by_usd_threshold():
    transfers = [
        Transfer(ts=NOW, amount=1_000_000.0, direction="out"),  # $2M >= $1M
        Transfer(ts=NOW, amount=1.0, direction="in"),           # $2 < $1M
    ]
    r = summarize_flows(transfers, price_usd=2.0, whale_usd=1_000_000.0, deadband=0.05)
    assert r["whale_tx_count"] == 1


def test_empty_transfers_is_neutral_no_whales():
    r = summarize_flows([], price_usd=2.0, whale_usd=1e6, deadband=0.05)
    assert r["flow_signal"] == NEUTRAL
    assert r["whale_tx_count"] == 0
    assert r["net_flow"] == 0


# ── collector: writes a row using the stored price ───────────────
def test_writes_one_row_converted_to_usd(conn):
    _seed_price(conn, "LINK/USDT", close=20.0)
    source = FakeSource([Transfer(ts=NOW, amount=100.0, direction="out")])
    written = _collector(conn, source).run_once()

    assert written == 1
    row = db.query_all(conn, "onchain_data")[0]
    assert row["symbol"] == "LINK/USDT"
    assert row["ts"] == NOW
    assert row["exchange_outflow"] == pytest.approx(2000.0)  # 100 * $20
    assert row["flow_signal"] == ACCUMULATION


# ── graceful degradation (FR-DC-4) ───────────────────────────────
def test_uncovered_asset_writes_nothing(conn):
    _seed_price(conn, "LINK/USDT", close=20.0)
    source = FakeSource(None)  # None == "asset not covered by this source"
    assert _collector(conn, source).run_once() == 0
    assert db.query_all(conn, "onchain_data") == []


def test_no_price_yet_writes_nothing(conn):
    # No market_data row seeded → cannot convert to USD → skip the cycle.
    source = FakeSource([Transfer(ts=NOW, amount=100.0, direction="out")])
    assert _collector(conn, source).run_once() == 0
    assert db.query_all(conn, "onchain_data") == []


# ── base-loop isolation + observe-only (FR-DC-4 / FR-DC-5) ────────
def test_run_loop_isolates_a_failing_cycle(conn):
    class Boom(BaseCollector):
        table = "onchain_data"

        def fetch(self):
            raise RuntimeError("source down")

        def normalize(self, raw):  # pragma: no cover - never reached
            return []

    c = Boom(conn, interval=0)
    stop = threading.Event()
    calls = {"n": 0}
    original = c.run_once

    def wrapped():
        calls["n"] += 1
        if calls["n"] >= 3:
            stop.set()
        return original()

    c.run_once = wrapped
    c.run(stop)  # returns instead of crashing → failure isolated


def test_collector_has_no_trading_api(conn):
    """FR-DC-5: a collector observes only — it exposes no order/trade methods."""
    c = _collector(conn, FakeSource([]))
    for forbidden in ("create_order", "open_trade", "buy", "sell", "execute"):
        assert not hasattr(c, forbidden)


# ── Etherscan payload parsing (pure) ─────────────────────────────
EXCHANGE = "0x28c6c06298d514db089934071355e5743bf21d60"


def _tx(ts, value, frm, to, decimals=18):
    return {"timeStamp": str(ts), "value": str(value), "from": frm, "to": to,
            "tokenDecimal": str(decimals)}


def test_parse_classifies_in_and_out():
    other = "0xabc0000000000000000000000000000000000001"
    payload = {
        "status": "1",
        "result": [
            _tx(NOW, 10 * 10**18, other, EXCHANGE),       # onto exchange → in
            _tx(NOW, 5 * 10**18, EXCHANGE, other),        # leaving exchange → out
        ],
    }
    transfers = parse_tokentx(payload, EXCHANGE, since_ts=NOW - 3600)
    assert {t.direction for t in transfers} == {"in", "out"}
    amounts = {t.direction: t.amount for t in transfers}
    assert amounts["in"] == pytest.approx(10.0)
    assert amounts["out"] == pytest.approx(5.0)


def test_parse_drops_transfers_before_window():
    other = "0xabc0000000000000000000000000000000000001"
    payload = {"status": "1", "result": [_tx(NOW - 99999, 10**18, other, EXCHANGE)]}
    assert parse_tokentx(payload, EXCHANGE, since_ts=NOW - 3600) == []


def test_parse_skips_malformed_and_handles_no_results():
    assert parse_tokentx({"status": "0", "result": []}, EXCHANGE, 0) == []
    bad = {"status": "1", "result": [{"timeStamp": "x", "value": "1"}]}
    assert parse_tokentx(bad, EXCHANGE, 0) == []


# ── Etherscan source coverage gate ───────────────────────────────
def test_etherscan_returns_none_for_uncovered_asset():
    src = EtherscanSource(api_key="dummy")
    assert src.transfers("BTC/USDT", "BTC", NOW - 3600, NOW) is None


def test_etherscan_unavailable_without_key():
    src = EtherscanSource(api_key="")  # covered asset but no key → unavailable
    assert src.transfers("LINK/USDT", "LINK", NOW - 3600, NOW) is None
