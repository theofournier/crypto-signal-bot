"""Phase 7 tests for the performance postmortem (learning/postmortem.py).

Proves the Phase 7 "done when": a readable summary from the journal, computing win
rate, avg win ÷ avg loss, net P&L after fees and max drawdown (FR-LE-1), with source
attribution that shows which source helps vs hurts on losers (FR-LE-3), and an honest
sample-size verdict (FR-LE-4).

Run with: pytest tests/test_postmortem.py
"""

from __future__ import annotations

import pytest

from data import db
from learning import postmortem

ENTRY_TS = 1_700_000_000
HOUR = 3600


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "storage.db")
    yield connection
    connection.close()


def add_trade(conn, *, pnl, win, exit_ts, mode="dry",
              market_sub=50.0, onchain_sub=50.0, sentiment_sub=50.0):
    """Insert a signal + its closed trade with the given outcome and sub-scores."""
    composite = (market_sub + onchain_sub + sentiment_sub) / 3.0
    sig_id = db.insert(conn, "signals", {
        "ts": exit_ts - HOUR, "symbol": "BTC/USDT", "market_sub": market_sub,
        "onchain_sub": onchain_sub, "sentiment_sub": sentiment_sub, "composite": composite,
        "direction": "long", "gate_passed": 1, "reason": "test",
    })
    entry_price = 100.0
    return db.insert(conn, "trades", {
        "signal_id": sig_id, "symbol": "BTC/USDT", "direction": "long", "mode": mode,
        "entry_ts": exit_ts - HOUR, "entry_price": entry_price, "size": 1.0,
        "stop_loss": 97.0, "take_profit": 106.0, "exit_ts": exit_ts,
        "exit_price": entry_price + pnl, "exit_reason": "take_profit" if win else "stop_loss",
        "pnl": pnl, "pnl_pct": pnl / entry_price * 100.0, "status": "closed", "win": win,
    })


# ── headline metrics (FR-LE-1) ────────────────────────────────────
def test_metrics_over_a_small_mixed_journal(conn):
    # Two wins (+30, +10), two losses (-15, -5): win rate 50%, net +20.
    add_trade(conn, pnl=30.0, win=1, exit_ts=ENTRY_TS + 1 * HOUR)
    add_trade(conn, pnl=-15.0, win=0, exit_ts=ENTRY_TS + 2 * HOUR)
    add_trade(conn, pnl=10.0, win=1, exit_ts=ENTRY_TS + 3 * HOUR)
    add_trade(conn, pnl=-5.0, win=0, exit_ts=ENTRY_TS + 4 * HOUR)

    rows = postmortem.closed_trades_with_signals(conn, mode="dry")
    m = postmortem.compute_metrics(rows, bankroll=1000.0)

    assert m.n_closed == 4 and m.n_wins == 2 and m.n_losses == 2
    assert m.win_rate == pytest.approx(0.5)
    assert m.avg_win == pytest.approx(20.0)      # (30 + 10) / 2
    assert m.avg_loss == pytest.approx(10.0)     # (15 + 5) / 2, positive magnitude
    assert m.win_loss_ratio == pytest.approx(2.0)
    assert m.net_pnl == pytest.approx(20.0)


def test_max_drawdown_tracks_the_worst_peak_to_trough(conn):
    # Equity from 1000: +30 -> 1030 (peak), -15 -> 1015, -5 -> 1010.
    # Worst drop from the 1030 peak is 20 (abs), 20/1030 (pct).
    add_trade(conn, pnl=30.0, win=1, exit_ts=ENTRY_TS + 1 * HOUR)
    add_trade(conn, pnl=-15.0, win=0, exit_ts=ENTRY_TS + 2 * HOUR)
    add_trade(conn, pnl=-5.0, win=0, exit_ts=ENTRY_TS + 3 * HOUR)

    rows = postmortem.closed_trades_with_signals(conn, mode="dry")
    m = postmortem.compute_metrics(rows, bankroll=1000.0)
    assert m.max_drawdown == pytest.approx(20.0)
    assert m.max_drawdown_pct == pytest.approx(20.0 / 1030.0)


def test_only_the_requested_mode_is_judged(conn):
    add_trade(conn, pnl=10.0, win=1, exit_ts=ENTRY_TS + 1 * HOUR, mode="dry")
    add_trade(conn, pnl=99.0, win=1, exit_ts=ENTRY_TS + 2 * HOUR, mode="live")
    rows = postmortem.closed_trades_with_signals(conn, mode="dry")
    m = postmortem.compute_metrics(rows, bankroll=1000.0)
    assert m.n_closed == 1 and m.net_pnl == pytest.approx(10.0)


# ── sample-size honesty (FR-LE-4) ─────────────────────────────────
def test_verdict_label_reflects_sample_size():
    assert "noise" in postmortem.verdict_label(5)
    assert "weak preliminary" in postmortem.verdict_label(50)
    assert "actionable verdict" in postmortem.verdict_label(150)


# ── source attribution (FR-LE-3) ──────────────────────────────────
def test_attribution_flags_the_source_that_hurts_on_losers(conn):
    # Winners: strong market, weak sentiment. Losers: weak market, strong sentiment.
    # => sentiment is more bullish on losers than winners → "hurting".
    add_trade(conn, pnl=20.0, win=1, exit_ts=ENTRY_TS + 1 * HOUR,
              market_sub=80.0, sentiment_sub=40.0)
    add_trade(conn, pnl=15.0, win=1, exit_ts=ENTRY_TS + 2 * HOUR,
              market_sub=78.0, sentiment_sub=42.0)
    add_trade(conn, pnl=-18.0, win=0, exit_ts=ENTRY_TS + 3 * HOUR,
              market_sub=45.0, sentiment_sub=85.0)
    add_trade(conn, pnl=-12.0, win=0, exit_ts=ENTRY_TS + 4 * HOUR,
              market_sub=47.0, sentiment_sub=83.0)

    rows = postmortem.closed_trades_with_signals(conn, mode="dry")
    attribution = {a.source: a for a in postmortem.compute_attribution(rows)}

    assert attribution["market"].verdict == "helping"      # higher on winners
    assert attribution["sentiment"].verdict == "hurting"   # higher on losers
    suspect = postmortem.prime_suspect(list(attribution.values()))
    assert suspect is not None and suspect.source == "sentiment"


def test_no_prime_suspect_when_sources_are_balanced(conn):
    add_trade(conn, pnl=10.0, win=1, exit_ts=ENTRY_TS + 1 * HOUR,
              market_sub=60.0, sentiment_sub=60.0)
    add_trade(conn, pnl=-10.0, win=0, exit_ts=ENTRY_TS + 2 * HOUR,
              market_sub=60.0, sentiment_sub=60.0)
    rows = postmortem.closed_trades_with_signals(conn, mode="dry")
    assert postmortem.prime_suspect(postmortem.compute_attribution(rows)) is None


# ── windowing + rendering ─────────────────────────────────────────
def test_days_window_excludes_older_trades(conn):
    now = ENTRY_TS + 100 * 86_400
    add_trade(conn, pnl=5.0, win=1, exit_ts=now - 2 * 86_400)   # within last 7 days
    add_trade(conn, pnl=99.0, win=1, exit_ts=now - 30 * 86_400)  # older — excluded
    report = postmortem.build_report(conn, mode="dry", days=7, bankroll=1000.0, now=now)
    assert "last 7 day(s)" in report
    assert "closed trades : 1" in report


def test_report_handles_an_empty_journal(conn):
    report = postmortem.build_report(conn, mode="dry", bankroll=1000.0)
    assert "No closed trades yet" in report
    assert "not financial advice" in report.lower()


def test_summary_contains_the_headline_metrics(conn):
    add_trade(conn, pnl=30.0, win=1, exit_ts=ENTRY_TS + 1 * HOUR)
    add_trade(conn, pnl=-10.0, win=0, exit_ts=ENTRY_TS + 2 * HOUR)
    report = postmortem.build_report(conn, mode="dry", bankroll=1000.0)
    for label in ("win rate", "avg win", "net P&L", "max drawdown", "source attribution"):
        assert label in report
