"""Phase 6 tests for the monitor loop and the five exits (execution/monitor.py).

Proves the Phase 6 "done when": dry trades open AND close, every closed row has a
correct ``exit_reason`` and P&L net of fees, and all five exit types have been
observed firing (FR-MX-1/2/4/5/6). Also covers the leakage guard (a position is
never judged against its own entry candle) and first-to-fire ordering (FR-MX-2).

Run with: pytest tests/test_monitor.py
"""

from __future__ import annotations

import pytest

from core import scoring
from data import db
from exchange.client import ExchangeClient, Fees
from execution import executor, monitor

ENTRY_TS = 1_700_000_000
HOUR = 3600


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "storage.db")
    yield connection
    connection.close()


def dry_client(prefer_maker=True):
    return ExchangeClient(
        fees=Fees(maker_pct=0.0010, taker_pct=0.0020, prefer_maker=prefer_maker),
        dry_run=True,
        now_fn=lambda: ENTRY_TS,
    )


def scoring_cfg(threshold=40.0):
    # Market-only weighting; low threshold so a bullish candle fires (gate passes).
    return scoring.scoring_config(
        {"scoring": {"weights": {"market": 1.0, "onchain": 0.0, "sentiment": 0.0},
                     "threshold": threshold, "require_agreement": True}}
    )


def exits(**over):
    base = dict(reward_risk_ratio=2.0, stop_method="fixed_pct", stop_fixed_pct=0.03,
                use_trailing_stop=True, max_hold_hours=48.0)
    base.update(over)
    return executor.ExitConfig(**base)


def candle(ts, *, symbol="BTC/USDT", open=100.0, high=101.0, low=99.0, close=100.0,
           rsi=80.0, vwap_distance=3.0, volume_ratio=1.4, bb_width=0.02):
    """A market_data row. Defaults are a strongly bullish candle (gate fires)."""
    return {
        "ts": ts, "symbol": symbol, "timeframe": "1h",
        "open": open, "high": high, "low": low, "close": close, "volume": 10.0,
        "rsi": rsi, "vwap_distance": vwap_distance, "volume_ratio": volume_ratio,
        "bb_width": bb_width, "bid_ask_imbalance": None,
    }


def open_trade(conn, *, entry_price=100.0, size=5.0, stop_loss=97.0, take_profit=106.0,
               entry_ts=ENTRY_TS, symbol="BTC/USDT"):
    sig_id = db.insert(conn, "signals", {
        "ts": entry_ts, "symbol": symbol, "market_sub": 80.0, "onchain_sub": 50.0,
        "sentiment_sub": 50.0, "composite": 80.0, "direction": "long",
        "gate_passed": 1, "reason": "test",
    })
    return db.insert(conn, "trades", {
        "signal_id": sig_id, "symbol": symbol, "direction": "long", "mode": "dry",
        "entry_ts": entry_ts, "entry_price": entry_price, "size": size,
        "stop_loss": stop_loss, "take_profit": take_profit, "status": "open",
    })


def run_monitor(conn, exits_cfg, **client_over):
    return monitor.monitor_open_trades(
        conn, dry_client(**client_over), scoring_cfg(), exits_cfg, "1h"
    )


def closed_trade(conn, trade_id):
    rows = db.query(conn, "SELECT * FROM trades WHERE id = ?", (trade_id,))
    return rows[0]


# ── the five exits each fire ─────────────────────────────────────
def test_stop_loss_fires_when_a_later_candle_pierces_the_stop(conn):
    tid = open_trade(conn, stop_loss=97.0, take_profit=106.0)
    db.insert(conn, "market_data", candle(ENTRY_TS + HOUR, low=96.0, high=99.0, close=97.5))
    assert run_monitor(conn, exits(use_trailing_stop=False)) == 1
    t = closed_trade(conn, tid)
    assert t["status"] == "closed" and t["exit_reason"] == "stop_loss"
    assert t["exit_price"] == pytest.approx(97.0)  # fills at the stop level
    assert t["win"] == 0 and t["pnl"] < 0


def test_take_profit_fires_when_a_later_candle_reaches_the_target(conn):
    tid = open_trade(conn, stop_loss=97.0, take_profit=106.0)
    db.insert(conn, "market_data", candle(ENTRY_TS + HOUR, low=100.0, high=107.0, close=106.5))
    assert run_monitor(conn, exits(use_trailing_stop=False)) == 1
    t = closed_trade(conn, tid)
    assert t["exit_reason"] == "take_profit"
    assert t["exit_price"] == pytest.approx(106.0)
    assert t["win"] == 1 and t["pnl"] > 0


def test_trailing_stop_fires_after_a_favorable_move_then_reversal(conn):
    # Entry 100, stop 97 (distance 3). Price runs to 110 (peak), trail = 110-3 = 107.
    # A later candle dips to 106 (< 107, > 97) → trailing stop, not the fixed stop.
    tid = open_trade(conn, stop_loss=97.0, take_profit=200.0)
    db.insert(conn, "market_data", candle(ENTRY_TS + HOUR, low=108.0, high=110.0, close=109.0))
    db.insert(conn, "market_data", candle(ENTRY_TS + 2 * HOUR, low=106.0, high=109.0, close=106.5))
    assert run_monitor(conn, exits(use_trailing_stop=True)) == 1
    t = closed_trade(conn, tid)
    assert t["exit_reason"] == "trailing_stop"
    assert t["exit_price"] == pytest.approx(107.0)  # peak 110 - stop distance 3
    assert t["win"] == 1  # exited well above entry


def test_signal_exit_fires_when_the_thesis_no_longer_holds(conn):
    # No price exit (well inside SL/TP), but a bearish candle re-scores below the
    # gate → thesis invalidated → signal exit at the candle close (FR-MX-4).
    tid = open_trade(conn, stop_loss=90.0, take_profit=120.0)
    db.insert(conn, "market_data", candle(
        ENTRY_TS + HOUR, low=99.0, high=101.0, close=100.0,
        rsi=20.0, vwap_distance=-3.0, volume_ratio=1.4))  # bearish → gate fails
    assert run_monitor(conn, exits(use_trailing_stop=False)) == 1
    t = closed_trade(conn, tid)
    assert t["exit_reason"] == "signal_exit"
    assert t["exit_price"] == pytest.approx(100.0)


def test_time_exit_fires_at_the_hard_cap(conn):
    # Still bullish and inside SL/TP, but max_hold elapsed → time exit (FR-MX-5).
    tid = open_trade(conn, stop_loss=90.0, take_profit=120.0)
    db.insert(conn, "market_data", candle(ENTRY_TS + 49 * HOUR, low=99.0, high=101.0, close=100.5))
    assert run_monitor(conn, exits(use_trailing_stop=False, max_hold_hours=48.0)) == 1
    t = closed_trade(conn, tid)
    assert t["exit_reason"] == "time_exit"
    assert t["exit_price"] == pytest.approx(100.5)


# ── first-to-fire ordering (FR-MX-2) ─────────────────────────────
def test_stop_loss_wins_over_take_profit_in_the_same_candle(conn):
    # A candle that touches both stop and target: pessimistically the stop fires.
    tid = open_trade(conn, stop_loss=97.0, take_profit=106.0)
    db.insert(conn, "market_data", candle(ENTRY_TS + HOUR, low=96.0, high=107.0, close=100.0))
    run_monitor(conn, exits(use_trailing_stop=False))
    assert closed_trade(conn, tid)["exit_reason"] == "stop_loss"


# ── leakage guard (NFR-TEST) ─────────────────────────────────────
def test_entry_candle_is_never_used_to_close(conn):
    # Only the entry candle exists; its own low pierces the stop, but it happened
    # before entry → must NOT close (no look-ahead into the past).
    tid = open_trade(conn, stop_loss=97.0, take_profit=106.0, entry_ts=ENTRY_TS)
    db.insert(conn, "market_data", candle(ENTRY_TS, low=90.0, high=107.0, close=100.0))
    assert run_monitor(conn, exits()) == 0
    assert closed_trade(conn, tid)["status"] == "open"


def test_held_when_no_exit_condition_is_met(conn):
    tid = open_trade(conn, stop_loss=90.0, take_profit=120.0)
    db.insert(conn, "market_data", candle(ENTRY_TS + HOUR, low=99.0, high=101.0, close=100.0))
    assert run_monitor(conn, exits(use_trailing_stop=False, max_hold_hours=48.0)) == 0
    assert closed_trade(conn, tid)["status"] == "open"


# ── P&L is net of BOTH fees (FR-EX-3) ────────────────────────────
def test_pnl_is_net_of_entry_and_exit_fees(conn):
    # Entry maker @100, exit at take-profit (maker) @106, size 5.
    #   gross   = (106 - 100) * 5            = 30.0
    #   entry fee = 100*5*0.0010             = 0.5  (maker)
    #   exit  fee = 106*5*0.0010             = 0.53 (maker, take-profit rests)
    #   pnl     = 30 - 0.5 - 0.53            = 28.97
    tid = open_trade(conn, entry_price=100.0, size=5.0, stop_loss=97.0, take_profit=106.0)
    db.insert(conn, "market_data", candle(ENTRY_TS + HOUR, low=100.0, high=107.0, close=106.5))
    run_monitor(conn, exits(use_trailing_stop=False), prefer_maker=True)
    t = closed_trade(conn, tid)
    assert t["pnl"] == pytest.approx(28.97)
    assert t["pnl_pct"] == pytest.approx(28.97 / 500.0 * 100.0)


def test_stop_exit_pays_the_taker_fee(conn):
    # Stop exits as a market (taker) order at the higher taker_pct (0.0020).
    #   gross   = (97 - 100) * 5             = -15.0
    #   entry fee = 100*5*0.0010             = 0.5  (maker entry)
    #   exit  fee = 97*5*0.0020              = 0.97 (taker exit)
    #   pnl     = -15 - 0.5 - 0.97           = -16.47
    tid = open_trade(conn, entry_price=100.0, size=5.0, stop_loss=97.0, take_profit=106.0)
    db.insert(conn, "market_data", candle(ENTRY_TS + HOUR, low=96.0, high=99.0, close=97.5))
    run_monitor(conn, exits(use_trailing_stop=False), prefer_maker=True)
    assert closed_trade(conn, tid)["pnl"] == pytest.approx(-16.47)
