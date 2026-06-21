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
    ChainConfig,
    CompositeSource,
    DefiLlamaSource,
    EtherscanSource,
    OnChainCollector,
    OnChainSource,
    Transfer,
    TransferSource,
    build_source,
    parse_tokentx,
    summarize_flows,
    tvl_reading,
)
from data import db

NOW = 1_704_067_200  # 2024-01-01 00:00:00 UTC (seconds)


class FakeSource(TransferSource):
    """A transfer source returning a fixed list (None == 'asset not covered')."""

    def __init__(self, transfers):
        self._transfers = transfers
        self.calls = 0

    def covers(self, base_asset):
        return self._transfers is not None

    def transfers(self, base_asset, since_ts, now_ts):
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
    assert src.covers("BTC") is False
    assert src.transfers("BTC", NOW - 3600, NOW) is None


def test_etherscan_unavailable_without_key():
    src = EtherscanSource(api_key="")  # covered asset but no key → unavailable
    assert src.covers("LINK") is True
    assert src.transfers("LINK", NOW - 3600, NOW) is None


# ── config-driven, multichain registry ───────────────────────────
def test_chainconfig_normalizes_case():
    c = ChainConfig.from_dict("bsc", {
        "chain_id": 56,
        "cex_addresses": ["0xABCDEF0000000000000000000000000000000001"],
        "tokens": {"foo": "0xFEED000000000000000000000000000000000002"},
    })
    assert c.chain_id == 56
    assert c.cex_addresses == ("0xabcdef0000000000000000000000000000000001",)
    assert c.tokens == {"FOO": "0xfeed000000000000000000000000000000000002"}  # key upper, val lower


def test_from_config_uses_custom_chains():
    cfg = {"chains": {"poly": {"chain_id": 137, "cex_addresses": [EXCHANGE],
                               "tokens": {"FOO": "0xfoo"}}}}
    src = EtherscanSource.from_config(cfg, api_key="k")
    assert [c.name for c in src.chains] == ["poly"]
    assert src.chains[0].chain_id == 137


def test_from_config_falls_back_to_defaults_when_no_chains():
    src = EtherscanSource.from_config({}, api_key="k")
    assert any(c.name == "ethereum" for c in src.chains)
    assert any("LINK" in c.tokens for c in src.chains)


def test_covered_config_token_is_queried(monkeypatch):
    """A token listed in config is queried (returns a list); an unlisted one is None."""
    cfg = {"chains": {"poly": {"chain_id": 137, "cex_addresses": [EXCHANGE],
                               "tokens": {"FOO": "0xfoo"}}}}
    src = EtherscanSource.from_config(cfg, api_key="k")
    monkeypatch.setattr(src, "_get_tokentx", lambda *a, **k: {"status": "1", "result": []})

    assert src.transfers("FOO", NOW - 3600, NOW) == []   # covered, no transfers
    assert src.transfers("BAR", NOW - 3600, NOW) is None  # not in registry


def test_asset_on_two_chains_is_summed(monkeypatch):
    """A token configured on two chains aggregates transfers from both."""
    cfg = {"chains": {
        "a": {"chain_id": 1, "cex_addresses": [EXCHANGE], "tokens": {"FOO": "0xa"}},
        "b": {"chain_id": 56, "cex_addresses": [EXCHANGE], "tokens": {"FOO": "0xb"}},
    }}
    src = EtherscanSource.from_config(cfg, api_key="k")
    seen_chains = []

    def fake_get(chain_id, address, contract):
        seen_chains.append(chain_id)
        return {"status": "1", "result": [_tx(NOW, 10**18, EXCHANGE, "0xother")]}

    monkeypatch.setattr(src, "_get_tokentx", fake_get)
    transfers = src.transfers("FOO", NOW - 3600, NOW)
    assert len(transfers) == 2            # one per chain
    assert sorted(seen_chains) == [1, 56]  # both chains queried


# ── DefiLlama source: chain TVL momentum ─────────────────────────
def _tvl(date, tvl):
    return {"date": str(date), "tvl": tvl}


def test_tvl_reading_rising_is_accumulation():
    pts = [_tvl(NOW - 86400, 1_000_000_000.0), _tvl(NOW, 1_200_000_000.0)]
    r = tvl_reading(pts, now_ts=NOW, lookback_seconds=86400, deadband=0.05)
    assert r["net_flow"] == pytest.approx(200_000_000.0)
    assert r["flow_signal"] == ACCUMULATION
    # a TVL source does not measure CEX flows / whales → honest NULLs
    assert r["exchange_inflow"] is None and r["whale_tx_count"] is None


def test_tvl_reading_falling_is_distribution():
    pts = [_tvl(NOW - 86400, 1_000_000_000.0), _tvl(NOW, 900_000_000.0)]
    r = tvl_reading(pts, now_ts=NOW, lookback_seconds=86400, deadband=0.05)
    assert r["flow_signal"] == DISTRIBUTION


def test_tvl_reading_flat_is_neutral_and_empty_is_none():
    pts = [_tvl(NOW - 86400, 1_000_000_000.0), _tvl(NOW, 1_010_000_000.0)]  # +1% < 5%
    assert tvl_reading(pts, NOW, 86400, 0.05)["flow_signal"] == NEUTRAL
    assert tvl_reading([], NOW, 86400, 0.05) is None


def test_defillama_covers_l1_not_erc20_governance():
    src = DefiLlamaSource()
    assert src.covers("SOL") is True      # native L1 → chain TVL
    assert src.covers("LINK") is False    # ERC-20 governance token → not a chain


def test_defillama_read_uses_tvl(monkeypatch):
    src = DefiLlamaSource(chains={"SOL": "Solana"}, lookback_seconds=86400)
    monkeypatch.setattr(src, "_get_chain_tvl",
                        lambda slug: [_tvl(NOW - 86400, 1e9), _tvl(NOW, 1.3e9)])
    r = src.read("SOL", NOW - 3600, NOW, price_usd=None, whale_usd=0, deadband=0.05)
    assert r["flow_signal"] == ACCUMULATION
    assert src.read("DOGE", NOW - 3600, NOW, price_usd=None, whale_usd=0, deadband=0.05) is None


# ── composing sources + build_source ─────────────────────────────
def test_composite_routes_to_first_covering_source():
    etherscan = EtherscanSource(api_key="k")              # covers LINK, not SOL
    defillama = DefiLlamaSource()                          # covers SOL, not LINK
    comp = CompositeSource([etherscan, defillama])
    assert comp.covers("LINK") and comp.covers("SOL")
    assert comp.covers("DOGE") is False


def test_build_source_assembles_providers():
    cfg = {"providers": ["etherscan", "defillama"]}
    src = build_source(cfg, api_key="k")
    assert isinstance(src, CompositeSource)
    assert src.covers("LINK")   # via etherscan
    assert src.covers("SOL")    # via defillama


def test_build_source_single_provider_is_not_wrapped():
    src = build_source({"providers": ["defillama"]})
    assert isinstance(src, DefiLlamaSource)


def test_build_source_unknown_provider_skipped():
    assert build_source({"providers": ["nope"]}) is None
