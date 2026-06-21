"""Phase 8 tests for the historical replay (learning/backtest.py).

Proves the Phase 8 backtest "done when": the replay runs stored candles through the
SAME engine, opens AND closes simulated trades, **deducts fees on every fill** (a
fee-free backtest is fiction), and is **leakage-safe** — a position is never closed
by a candle at or before its own entry, so no future price feeds a past decision
(NFR-TEST / G4). Also covers the empty-history case.

Run with: pytest tests/test_backtest.py
"""

from __future__ import annotations

import pytest

from data import db
from learning import backtest

START_TS = 1_700_000_000
HOUR = 3600


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "storage.db")
    yield connection
    connection.close()


def cfg(**over):
    """A market-only, low-threshold config so a bullish candle fires and trades.

    Volatility filter and trailing stop are off so the exit path is deterministic;
    a fixed-percent stop avoids needing 15 candles of ATR warm-up. Maker/taker fees
    differ so the fee on a fill is unmistakably non-zero.
    """
    base = {
        "mode": {"dry_run": True},
        "scoring": {"weights": {"market": 1.0, "onchain": 0.0, "sentiment": 0.0},
                    "threshold": 40.0, "require_agreement": True},
        "risk": {"kelly_fraction": 0.25, "max_open_positions": 3,
                 "volatility_filter": False, "min_edge_over_fees": 1.5,
                 "bankroll": 10_000.0, "default_win_rate": 0.50,
                 "default_reward_risk": 2.0},
        "fees": {"maker_pct": 0.0010, "taker_pct": 0.0020, "prefer_maker": True},
        "exits": {"reward_risk_ratio": 2.0, "stop_method": "fixed_pct",
                  "stop_fixed_pct": 0.03, "use_trailing_stop": False,
                  "max_hold_hours": 48},
    }
    base.update(over)
    return base


def candle(ts, *, symbol="BTC/USDT", open=100.0, high=101.0, low=99.0, close=100.0,
           rsi=80.0, vwap_distance=3.0, volume_ratio=1.4, bb_width=0.02):
    """A market_data row. Defaults are a strongly bullish candle (gate fires)."""
    return {
        "ts": ts, "symbol": symbol, "timeframe": "1h",
        "open": open, "high": high, "low": low, "close": close, "volume": 10.0,
        "volume_ratio": volume_ratio, "bid_ask_imbalance": None,
        "bb_width": bb_width, "rsi": rsi, "vwap_distance": vwap_distance,
    }


def seed(conn, rows):
    for row in rows:
        db.insert(conn, "market_data", row)


def test_opens_and_closes_a_trade_with_fees_deducted(conn):
    # Candle 0 fires and opens at close=100 (SL 97, TP 106 on a 3% fixed stop).
    # Candle 1's high clears the take-profit, closing the position.
    seed(conn, [
        candle(START_TS, high=101.0, low=100.0, close=100.0),
        candle(START_TS + HOUR, high=110.0, low=100.0, close=105.0),
    ])

    result = backtest.backtest(conn, "BTC/USDT", "1h", cfg())

    assert result.n_trades == 1
    m = result.metrics
    assert m.n_wins == 1 and m.n_losses == 0
    # Size = bankroll(10000) * kelly(0.25) * edge(0.25) / entry(100) = 6.25.
    # Gross = (106 - 100) * 6.25 = 37.50. Fees: entry maker 100*6.25*0.001 = 0.625,
    # TP is a maker exit 106*6.25*0.001 = 0.6625. Net = 37.50 - 0.625 - 0.6625.
    gross = (106.0 - 100.0) * 6.25
    assert m.net_pnl == pytest.approx(gross - 0.625 - 0.6625)
    assert m.net_pnl < gross  # fees were deducted — not a fee-free fiction


def test_leakage_entry_candle_high_does_not_trigger_exit(conn):
    # Entry candle has a huge high (200) that already exceeds the TP (106). A
    # leaky backtest would "see" that high and take profit on the entry candle.
    # Leakage-safe: the position must survive its own entry candle and only close
    # on the LATER candle that legitimately reaches the target.
    seed(conn, [
        candle(START_TS, high=200.0, low=99.0, close=100.0),          # entry; high ignored
        candle(START_TS + HOUR, high=101.0, low=100.0, close=100.5),  # no exit
        candle(START_TS + 2 * HOUR, high=110.0, low=100.0, close=105.0),  # TP here
    ])

    result = backtest.backtest(conn, "BTC/USDT", "1h", cfg())

    assert result.n_trades == 1
    # The single closed trade must have exited on candle 2, not candle 0. Re-run the
    # replay against an isolated scratch so we can inspect the entry/exit timestamps.
    trade = _single_closed_trade(conn)
    assert trade["exit_reason"] == "take_profit"
    assert trade["entry_ts"] == START_TS
    assert trade["exit_ts"] == START_TS + 2 * HOUR  # not the entry candle


def test_empty_history_yields_no_trades(conn):
    result = backtest.backtest(conn, "BTC/USDT", "1h", cfg())
    assert result.n_candles == 0
    assert result.n_trades == 0
    assert "No closed trades" in result.report


def _single_closed_trade(conn):
    """Replay against an isolated scratch and return the one closed trade row.

    Mirrors backtest.run_replay's scratch wiring so the test can inspect entry/exit
    timestamps directly (run_replay returns metrics, not raw rows).
    """
    from core import risk, scoring
    from exchange.client import ExchangeClient, Fees
    from execution import executor, monitor

    c = cfg()
    rows = backtest.load_market_rows(conn, "BTC/USDT", "1h")
    scoring_cfg = scoring.scoring_config(c)
    exits_cfg = executor.ExitConfig.from_config(c)
    clock = {"ts": 0}
    client = ExchangeClient(fees=Fees.from_config(c), dry_run=True, now_fn=lambda: clock["ts"])
    scratch = db.connect(":memory:")
    gate = risk.RiskGate(scratch, risk.RiskConfig.from_config(c), mode=client.mode)
    for cd in rows:
        clock["ts"] = int(cd["ts"])
        db.insert(scratch, "market_data", backtest._market_insert_row(cd))
        ev = scoring.evaluate(cd, scoring_cfg)
        sid = db.insert(scratch, "signals", ev.as_row())
        if ev.gate_passed:
            executor.try_open_from_signal(
                scratch, client, gate, exits_cfg, sid, ev.as_row(), cd,
                backtest._recent_market_rows(scratch, "BTC/USDT", "1h"),
                now_fn=lambda: clock["ts"],
            )
        monitor.monitor_open_trades(scratch, client, scoring_cfg, exits_cfg, "1h")
    closed = db.query(scratch, "SELECT * FROM trades WHERE status = 'closed' ORDER BY id")
    scratch.close()
    assert len(closed) == 1
    return closed[0]
