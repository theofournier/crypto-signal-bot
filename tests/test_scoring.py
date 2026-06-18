"""Phase 4 tests for the scoring engine (core/normalize.py, core/scoring.py,
scripts/run_engine.py).

Network-free and DB-backed via a temp storage.db. They prove the Phase 4
non-negotiables: market features map to a sensible 0-100 sub-score + direction;
on-chain/sentiment are inactive placeholders that neither vote nor block; the gate
fires only on (composite >= threshold AND active sources agree long); v1 only ever
emits "long"; every evaluation — firing or not — is persisted with a reason; and a
candle is scored once (no duplicate signals).

Run with: pytest tests/test_scoring.py
"""

from __future__ import annotations

import pytest

from core import normalize, scoring
from core.normalize import BEARISH, LONG, NONE
from data import db

import importlib.util
import sys

# Load scripts/run_engine.py (not a package) for engine-level tests. Registering
# it in sys.modules before exec lets its @dataclass resolve string annotations
# (PEP 563) — dataclass looks the defining module up there.
_ENGINE_PATH = __import__("pathlib").Path(__file__).resolve().parent.parent / "scripts" / "run_engine.py"
_spec = importlib.util.spec_from_file_location("run_engine", _ENGINE_PATH)
run_engine = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = run_engine
_spec.loader.exec_module(run_engine)


# ── fixtures / helpers ───────────────────────────────────────────
@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "storage.db")
    yield connection
    connection.close()


def market_row(ts=1_700_000_000, symbol="BTC/USDT", timeframe="1h", **features):
    """A market_data row with neutral defaults; override features per test."""
    base = {
        "ts": ts, "symbol": symbol, "timeframe": timeframe,
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 10.0,
        "rsi": 50.0, "vwap_distance": 0.0, "volume_ratio": 1.0,
        "bb_width": 0.05, "bid_ask_imbalance": None,
    }
    base.update(features)
    return base


def cfg(threshold=72.0, require_agreement=True, weights=None):
    return scoring.scoring_config(
        {
            "scoring": {
                "weights": weights or {"market": 0.40, "onchain": 0.25, "sentiment": 0.35},
                "threshold": threshold,
                "require_agreement": require_agreement,
            }
        }
    )


# ── normalize: market sub-score + direction ──────────────────────
def test_neutral_features_score_neutral_no_direction():
    sub = normalize.market_subscore(market_row(rsi=50.0, vwap_distance=0.0, volume_ratio=1.0))
    assert sub.score == pytest.approx(normalize.NEUTRAL)
    assert sub.direction == NONE
    assert sub.active is True


def test_bullish_features_score_high_and_long():
    sub = normalize.market_subscore(market_row(rsi=72.0, vwap_distance=2.0, volume_ratio=1.4))
    assert sub.score > normalize.NEUTRAL + normalize.DIRECTION_BAND
    assert sub.direction == LONG


def test_bearish_features_score_low_and_bearish():
    sub = normalize.market_subscore(market_row(rsi=28.0, vwap_distance=-2.0, volume_ratio=1.4))
    assert sub.score < normalize.NEUTRAL - normalize.DIRECTION_BAND
    assert sub.direction == BEARISH


def test_missing_features_fall_back_to_neutral_not_a_fabricated_direction():
    sub = normalize.market_subscore(market_row(rsi=None, vwap_distance=None, volume_ratio=None))
    assert sub.score == pytest.approx(normalize.NEUTRAL)
    assert sub.direction == NONE


def test_thin_volume_damps_a_bullish_reading_toward_neutral():
    strong = normalize.market_subscore(market_row(rsi=70.0, vwap_distance=2.0, volume_ratio=1.5))
    thin = normalize.market_subscore(market_row(rsi=70.0, vwap_distance=2.0, volume_ratio=0.5))
    assert thin.score < strong.score  # same signal, less conviction on thin volume


def test_score_is_clamped_to_0_100():
    hi = normalize.market_subscore(market_row(rsi=100.0, vwap_distance=50.0, volume_ratio=10.0))
    lo = normalize.market_subscore(market_row(rsi=0.0, vwap_distance=-50.0, volume_ratio=10.0))
    assert 0.0 <= lo.score <= 100.0
    assert 0.0 <= hi.score <= 100.0


def test_onchain_and_sentiment_are_inactive_placeholders():
    for sub in (normalize.onchain_subscore(), normalize.sentiment_subscore()):
        assert sub.active is False
        assert sub.direction == NONE
        assert sub.score == pytest.approx(normalize.NEUTRAL)


# ── scoring: composite + gate ────────────────────────────────────
def test_composite_uses_configured_weights():
    ev = scoring.evaluate(market_row(rsi=80.0, vwap_distance=3.0, volume_ratio=1.5), cfg())
    expected = (
        0.40 * ev.market_sub + 0.25 * ev.onchain_sub + 0.35 * ev.sentiment_sub
    )
    assert ev.composite == pytest.approx(expected)


def test_gate_fires_only_when_above_threshold_and_agreeing():
    # Very low threshold so a strong long market reading clears it; only market is
    # active, so agreement == market direction (BUILD_PLAN Phase 4 note).
    ev = scoring.evaluate(
        market_row(rsi=85.0, vwap_distance=4.0, volume_ratio=1.5), cfg(threshold=40.0)
    )
    assert ev.gate_passed is True
    assert ev.direction == LONG
    assert "FIRED long" in ev.reason


def test_below_threshold_does_not_fire_and_records_reason():
    ev = scoring.evaluate(
        market_row(rsi=85.0, vwap_distance=4.0, volume_ratio=1.5), cfg(threshold=99.0)
    )
    assert ev.gate_passed is False
    assert ev.direction == NONE
    assert "<" in ev.reason and "threshold" in ev.reason


def test_bearish_market_never_emits_a_short():
    # Above threshold by score magnitude is impossible here, but even a low
    # threshold must not fire on a bearish reading (long-or-flat, FR-EX-4).
    ev = scoring.evaluate(
        market_row(rsi=15.0, vwap_distance=-4.0, volume_ratio=1.5), cfg(threshold=1.0)
    )
    assert ev.gate_passed is False
    assert ev.direction == NONE  # never "short" / never "bearish" as an action
    assert "agree long" in ev.reason


def test_require_agreement_false_fires_on_threshold_alone():
    ev = scoring.evaluate(
        market_row(rsi=20.0, vwap_distance=-1.0, volume_ratio=1.0),
        cfg(threshold=1.0, require_agreement=False),
    )
    assert ev.gate_passed is True
    assert ev.direction == LONG


# ── engine: persistence + dedup ──────────────────────────────────
def test_engine_writes_a_signal_row_for_every_evaluation(conn):
    db.insert(conn, "market_data", market_row(ts=1_700_000_000))
    written = run_engine.evaluate_pair(conn, "BTC/USDT", "1h", cfg(threshold=40.0))
    assert written is True

    rows = db.query_all(conn, "signals")
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "BTC/USDT"
    assert row["ts"] == 1_700_000_000
    assert row["reason"]  # human-readable explanation is always present
    assert row["gate_passed"] in (0, 1)


def test_engine_does_not_duplicate_a_signal_for_the_same_candle(conn):
    db.insert(conn, "market_data", market_row(ts=1_700_000_000))
    assert run_engine.evaluate_pair(conn, "BTC/USDT", "1h", cfg()) is True
    assert run_engine.evaluate_pair(conn, "BTC/USDT", "1h", cfg()) is False
    assert len(db.query_all(conn, "signals")) == 1

    # A newer candle is a new evaluation -> a new signal row.
    db.insert(conn, "market_data", market_row(ts=1_700_003_600))
    assert run_engine.evaluate_pair(conn, "BTC/USDT", "1h", cfg()) is True
    assert len(db.query_all(conn, "signals")) == 2


def test_engine_skips_a_pair_with_no_market_data(conn):
    assert run_engine.evaluate_pair(conn, "ETH/USDT", "1h", cfg()) is False
    assert db.query_all(conn, "signals") == []
