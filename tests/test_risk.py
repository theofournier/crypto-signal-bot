"""Phase 5 tests for the risk gate (core/risk.py).

Proves the veto checks fire in the right conditions and that blocking is a normal,
logged outcome (FR-RM-5): exposure (FR-RM-1), drawdown (FR-RM-1), volatility spike
(FR-RM-2), the fee-aware edge filter (FR-RM-4), and fractional-Kelly sizing from the
journal (FR-RM-3) with config defaults until ≥ 100 closed trades (FR-LE-4).

Run with: pytest tests/test_risk.py
"""

from __future__ import annotations

import pytest

from core import risk
from data import db


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "storage.db")
    yield connection
    connection.close()


def rc(**over):
    base = dict(
        kelly_fraction=0.25, max_open_positions=3, max_drawdown_pause=0.15,
        volatility_filter=True, min_edge_over_fees=1.5, default_win_rate=0.50,
        default_reward_risk=2.0, bankroll=10_000.0, volatility_spike_mult=2.5,
    )
    base.update(over)
    return risk.RiskConfig(**base)


def gate(conn, **over):
    return risk.RiskGate(conn, rc(**over), mode="dry")


def open_trade_row(symbol="ETH/USDT", status="open", pnl=None, mode="dry", exit_ts=0):
    return {
        "signal_id": None, "symbol": symbol, "direction": "long", "mode": mode,
        "entry_ts": 1, "entry_price": 100.0, "size": 1.0, "stop_loss": 95.0,
        "take_profit": 110.0, "exit_ts": exit_ts, "exit_price": None,
        "exit_reason": None, "pnl": pnl, "pnl_pct": None, "status": status, "win": None,
    }


# A generous take-profit so the fee filter passes by default (5% move >> fees).
TP = 105.0
ENTRY = 100.0
FEE = 0.0020  # round-trip


def test_approves_and_sizes_with_default_edge(conn):
    d = gate(conn).assess("BTC/USDT", "1h", ENTRY, TP, FEE, bb_width=None)
    assert d.approved is True
    # edge = 0.5 - 0.5/2 = 0.25; notional = 10000 * 0.25 * 0.25 = 625; size = 625/100
    assert d.size == pytest.approx(625.0 / ENTRY)
    assert d.win_rate == 0.50 and d.reward_risk == 2.0


def test_blocks_when_max_open_positions_reached(conn):
    for _ in range(3):
        db.insert(conn, "trades", open_trade_row(status="open"))
    d = gate(conn, max_open_positions=3).assess("BTC/USDT", "1h", ENTRY, TP, FEE)
    assert d.approved is False and "exposure" in d.reason


def test_blocks_when_drawdown_exceeds_pause(conn):
    # Bankroll 10k; a closed -2000 trade is a 20% drawdown > 15% pause.
    db.insert(conn, "trades", open_trade_row(status="closed", pnl=-2000.0, exit_ts=10))
    d = gate(conn, max_drawdown_pause=0.15).assess("BTC/USDT", "1h", ENTRY, TP, FEE)
    assert d.approved is False and "drawdown" in d.reason


def test_blocks_on_volatility_spike(conn):
    # Seed a calm baseline of bb_width ~0.02, then present a 10x spike.
    for i in range(20):
        row = {"ts": 1_700_000_000 + i * 3600, "symbol": "BTC/USDT", "timeframe": "1h",
               "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1,
               "bb_width": 0.02, "rsi": 50, "vwap_distance": 0.0, "volume_ratio": 1.0,
               "bid_ask_imbalance": None}
        db.insert(conn, "market_data", row)
    d = gate(conn).assess("BTC/USDT", "1h", ENTRY, TP, FEE, bb_width=0.2)
    assert d.approved is False and "volatility" in d.reason


def test_volatility_filter_quiet_when_no_spike(conn):
    for i in range(20):
        row = {"ts": 1_700_000_000 + i * 3600, "symbol": "BTC/USDT", "timeframe": "1h",
               "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1,
               "bb_width": 0.02, "rsi": 50, "vwap_distance": 0.0, "volume_ratio": 1.0,
               "bid_ask_imbalance": None}
        db.insert(conn, "market_data", row)
    d = gate(conn).assess("BTC/USDT", "1h", ENTRY, TP, FEE, bb_width=0.025)
    assert d.approved is True


def test_fee_filter_rejects_a_move_that_barely_clears_fees(conn):
    # TP only 0.1% above entry; round-trip fee 0.2% x 1.5 margin = 0.3% required.
    tiny_tp = ENTRY * 1.001
    d = gate(conn).assess("BTC/USDT", "1h", ENTRY, tiny_tp, FEE, bb_width=None)
    assert d.approved is False and "fee filter" in d.reason


def test_blocks_when_edge_is_not_positive(conn):
    # win_rate 0.30, RR 1.0 => edge = 0.30 - 0.70 = -0.40 (negative) => block.
    d = gate(conn, default_win_rate=0.30, default_reward_risk=1.0).assess(
        "BTC/USDT", "1h", ENTRY, TP, FEE
    )
    assert d.approved is False and "edge" in d.reason


def test_uses_journal_stats_once_enough_trades_are_closed(conn):
    # 100 closed trades: 60 wins of +200, 40 losses of -100 => win_rate 0.6, RR 2.0.
    for i in range(60):
        db.insert(conn, "trades", open_trade_row(status="closed", pnl=200.0, exit_ts=i))
    for i in range(40):
        db.insert(conn, "trades", open_trade_row(status="closed", pnl=-100.0, exit_ts=100 + i))
    d = gate(conn, max_drawdown_pause=1.0).assess("BTC/USDT", "1h", ENTRY, TP, FEE)
    assert d.win_rate == pytest.approx(0.60)
    assert d.reward_risk == pytest.approx(2.0)
    assert d.approved is True


def test_below_sample_threshold_keeps_using_defaults(conn):
    # A handful of closed trades is noise (FR-LE-4) — defaults still apply.
    for i in range(5):
        db.insert(conn, "trades", open_trade_row(status="closed", pnl=200.0, exit_ts=i))
    d = gate(conn).assess("BTC/USDT", "1h", ENTRY, TP, FEE)
    assert d.win_rate == 0.50 and d.reward_risk == 2.0
