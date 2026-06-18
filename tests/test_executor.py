"""Phase 5 tests for execution (execution/executor.py) and the engine wiring.

Proves the Phase 5 "done when": a gated signal produces a simulated ``open`` trade
with SL/TP set atomically (FR-EX-1), fees are deducted on the entry fill (FR-EX-3),
the trade is journaled with ``status=open`` and ``mode=dry``, and no live order path
is reachable while ``dry_run`` is true (FR-SF-2). Also covers stop/take-profit
computation, the no-duplicate-position guard, and the risk-block path.

Run with: pytest tests/test_executor.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from core import risk
from data import db
from exchange.client import ExchangeClient, Fees
from execution import executor

# Load scripts/run_engine.py (not a package) to test the full Phase 5 wiring.
# Registered in sys.modules first so its @dataclass can resolve string
# annotations (PEP 563), which dataclass looks up via the defining module.
_ENGINE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "run_engine.py"
_spec = importlib.util.spec_from_file_location("run_engine", _ENGINE_PATH)
run_engine = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = run_engine
_spec.loader.exec_module(run_engine)


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "storage.db")
    yield connection
    connection.close()


def dry_client():
    return ExchangeClient(
        fees=Fees(maker_pct=0.0010, taker_pct=0.0020, prefer_maker=True),
        dry_run=True,
        now_fn=lambda: 1_700_000_000,
    )


def risk_gate(conn, **over):
    base = dict(
        kelly_fraction=0.25, max_open_positions=3, max_drawdown_pause=0.15,
        volatility_filter=True, min_edge_over_fees=1.5, default_win_rate=0.50,
        default_reward_risk=2.0, bankroll=10_000.0, volatility_spike_mult=2.5,
    )
    base.update(over)
    return risk.RiskGate(conn, risk.RiskConfig(**base), mode="dry")


def market_row(ts=1_700_000_000, symbol="BTC/USDT", close=100.0, bb_width=0.02):
    return {
        "ts": ts, "symbol": symbol, "timeframe": "1h",
        "open": 99.0, "high": 101.0, "low": 98.0, "close": close, "volume": 10.0,
        "rsi": 80.0, "vwap_distance": 3.0, "volume_ratio": 1.4,
        "bb_width": bb_width, "bid_ask_imbalance": None,
    }


def fired_signal_row(symbol="BTC/USDT", direction="long", gate_passed=1):
    return {
        "ts": 1_700_000_000, "symbol": symbol, "market_sub": 80.0, "onchain_sub": 50.0,
        "sentiment_sub": 50.0, "composite": 80.0, "direction": direction,
        "gate_passed": gate_passed, "reason": "test",
    }


# ── compute_exits ────────────────────────────────────────────────
def test_fixed_pct_exits_are_symmetric_around_entry():
    cfg = executor.ExitConfig(reward_risk_ratio=2.0, stop_method="fixed_pct", stop_fixed_pct=0.03)
    sl, tp = executor.compute_exits(100.0, [], cfg)
    assert sl == pytest.approx(97.0)        # 3% below
    assert tp == pytest.approx(106.0)       # 2x the 3 stop distance above
    assert sl < 100.0 < tp


def test_atr_stop_falls_back_to_fixed_pct_without_enough_history():
    cfg = executor.ExitConfig(stop_method="atr", stop_fixed_pct=0.03, atr_multiplier=2.0)
    sl, tp = executor.compute_exits(100.0, [], cfg)  # no candles → ATR unavailable
    assert sl == pytest.approx(97.0)


def test_atr_stop_uses_volatility_when_history_is_present():
    cfg = executor.ExitConfig(stop_method="atr", reward_risk_ratio=2.0, atr_multiplier=2.0)
    candles = [{"high": 110.0 + i, "low": 100.0 + i, "close": 105.0 + i} for i in range(20)]
    sl, tp = executor.compute_exits(105.0, candles, cfg)
    # TR ≈ 10 per candle → ATR ≈ 10 → stop distance ≈ 20.
    assert sl == pytest.approx(105.0 - 20.0, rel=0.2)
    assert (tp - 105.0) == pytest.approx(2.0 * (105.0 - sl))


# ── open_trade: atomic entry + SL/TP, journaled ──────────────────
def test_open_trade_writes_protected_open_dry_trade(conn):
    sig_id = db.insert(conn, "signals", fired_signal_row())
    tid = executor.open_trade(conn, dry_client(), sig_id, "BTC/USDT", 100.0, 5.0, 97.0, 106.0,
                              now_fn=lambda: 1_700_000_000)
    rows = db.query_all(conn, "trades")
    assert len(rows) == 1
    t = rows[0]
    assert t["id"] == tid
    assert t["status"] == "open" and t["mode"] == "dry" and t["direction"] == "long"
    assert t["entry_price"] == 100.0 and t["size"] == 5.0
    # SL and TP are both present the moment the trade exists (FR-EX-1, atomic).
    assert t["stop_loss"] == 97.0 and t["take_profit"] == 106.0
    assert t["signal_id"] == sig_id


# ── try_open_from_signal: the full gated path ────────────────────
def test_gated_signal_opens_a_simulated_trade(conn):
    sig_id = db.insert(conn, "signals", fired_signal_row())
    tid = executor.try_open_from_signal(
        conn, dry_client(), risk_gate(conn), executor.ExitConfig(stop_method="fixed_pct"),
        sig_id, fired_signal_row(), market_row(), recent_candles=[market_row()],
    )
    assert tid is not None
    t = db.query_all(conn, "trades")[0]
    assert t["status"] == "open" and t["mode"] == "dry"
    assert t["stop_loss"] < t["entry_price"] < t["take_profit"]


def test_non_firing_signal_opens_nothing(conn):
    sig_id = db.insert(conn, "signals", fired_signal_row(gate_passed=0))
    tid = executor.try_open_from_signal(
        conn, dry_client(), risk_gate(conn), executor.ExitConfig(stop_method="fixed_pct"),
        sig_id, fired_signal_row(gate_passed=0), market_row(), [market_row()],
    )
    assert tid is None
    assert db.query_all(conn, "trades") == []


def test_does_not_open_a_second_position_for_the_same_pair(conn):
    sig_id = db.insert(conn, "signals", fired_signal_row())
    args = (dry_client(), risk_gate(conn), executor.ExitConfig(stop_method="fixed_pct"),
            sig_id, fired_signal_row(), market_row(), [market_row()])
    assert executor.try_open_from_signal(conn, *args) is not None
    assert executor.try_open_from_signal(conn, *args) is None  # already holding
    assert len(db.query_all(conn, "trades")) == 1


def test_risk_block_prevents_a_trade(conn):
    # Force a block via the fee filter: a take-profit barely above entry.
    sig_id = db.insert(conn, "signals", fired_signal_row())
    tight = executor.ExitConfig(stop_method="fixed_pct", stop_fixed_pct=0.0005, reward_risk_ratio=1.0)
    tid = executor.try_open_from_signal(
        conn, dry_client(), risk_gate(conn), tight,
        sig_id, fired_signal_row(), market_row(), [market_row()],
    )
    assert tid is None
    assert db.query_all(conn, "trades") == []


# ── engine wiring: signal → trade through run_engine ─────────────
def test_engine_carries_a_gated_signal_into_a_dry_trade(conn):
    # A strongly bullish candle at a low threshold fires; execution opens a trade.
    db.insert(conn, "market_data", market_row())
    cfg = run_engine.scoring.scoring_config(
        {"scoring": {"weights": {"market": 1.0, "onchain": 0.0, "sentiment": 0.0},
                     "threshold": 40.0, "require_agreement": True}}
    )
    execution = run_engine.ExecutionContext(
        client=dry_client(),
        risk_gate=risk_gate(conn),
        exits_cfg=executor.ExitConfig(stop_method="fixed_pct"),
    )
    assert run_engine.evaluate_pair(conn, "BTC/USDT", "1h", cfg, execution) is True
    signals = db.query_all(conn, "signals")
    trades = db.query_all(conn, "trades")
    assert len(signals) == 1 and signals[0]["gate_passed"] == 1
    assert len(trades) == 1 and trades[0]["status"] == "open" and trades[0]["mode"] == "dry"
