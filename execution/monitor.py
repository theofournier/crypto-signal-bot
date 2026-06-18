"""Watch open positions and race the five exits to a close (BUILD_PLAN Phase 6).

The executor (Phase 5) opens a protected position and defines every exit level *at
entry*; this module is what later watches the market and closes the position when
the **first** of its exit conditions is met (FR-MX-1/2, PLAN §5.5). All five exits
are defined here — none is improvised after the fact:

  | Exit          | Fires when (for a long)                                        |
  |---------------|----------------------------------------------------------------|
  | stop_loss     | a later candle's low reaches the stop level ("I was wrong")     |
  | trailing_stop | price reversed after moving in favor; stop ratchets up, never down |
  | take_profit   | a later candle's high reaches the reward:risk target            |
  | signal_exit   | re-running scoring no longer fires — the entry thesis broke (FR-MX-4) |
  | time_exit     | max_hold_hours elapsed — the hard cap, always active (FR-MX-5)   |

**First to fire wins (FR-MX-2).** Within a single closed candle several conditions
can be true at once and we cannot see the intra-candle path, so the checks are
ordered **pessimistically**: the adverse price exits (stop, then trailing) are
assumed to fire before the favorable one (take-profit). The price exits (intra-
candle) are tested before the close-time exits (signal/time). This avoids the
optimistic bias that would make a backtest lie.

**Leakage-safe (NFR-TEST).** A position is only judged against candles that closed
**strictly after** its entry candle — the entry candle's own high/low happened
*before* we entered at its close, so using them would be look-ahead. Time is
measured on the **market clock** (candle ``ts``), not wall-clock, so dry-run, live,
and backtest behave identically (FR-SF-3, G4).

**Fees on every fill (FR-EX-3).** The exit fill goes through ``exchange/client`` so
the configured fee is deducted exactly as at entry. The realized ``pnl`` written to
the journal is net of **both** the entry and the exit fee; the entry fee is
reconstructed from how the entry was placed, so no extra schema column is needed.
This module reads/writes only the DB and the client — it never calls a collector or
the engine directly (FR-DP-2).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Mapping, Sequence

from core import scoring
from data import db
from exchange.client import LIMIT, MARKET, SELL, ExchangeClient
from execution.executor import ExitConfig

log = logging.getLogger("monitor")

SECONDS_PER_HOUR = 3600

# Exit reason labels stored on the trade row (PLAN §5.5).
STOP_LOSS = "stop_loss"
TRAILING_STOP = "trailing_stop"
TAKE_PROFIT = "take_profit"
SIGNAL_EXIT = "signal_exit"
TIME_EXIT = "time_exit"


@dataclass
class ExitResult:
    """Which exit fired, the price it fills at, the market time, and its fee class.

    ``order_type`` decides the exit fee (PLAN §5.6): a take-profit rests as a maker
    (limit) order; every other exit leaves as a taker (market) order.
    """

    reason: str
    exit_price: float
    exit_ts: int
    order_type: str


def open_trades(conn, mode: str) -> list:
    """Every still-open trade for this mode (dry/live), oldest first."""
    return db.query(
        conn,
        "SELECT * FROM trades WHERE status = 'open' AND mode = ? ORDER BY id",
        (mode,),
    )


def latest_candle(conn, symbol: str, timeframe: str):
    """The most recent closed candle for a pair, or None (FR-DC-6: closed only)."""
    rows = db.query(
        conn,
        "SELECT * FROM market_data WHERE symbol = ? AND timeframe = ? "
        "ORDER BY ts DESC LIMIT 1",
        (symbol, timeframe),
    )
    return rows[0] if rows else None


def peak_high_since(conn, symbol: str, timeframe: str, entry_ts: int) -> float | None:
    """Highest high over candles that closed **after** entry (favorable excursion).

    Strictly ``> entry_ts`` so the entry candle's own (pre-entry) high never feeds
    the trailing stop — that would be look-ahead. Returns None when no later candle
    exists yet.
    """
    rows = db.query(
        conn,
        "SELECT MAX(high) AS peak FROM market_data "
        "WHERE symbol = ? AND timeframe = ? AND ts > ?",
        (symbol, timeframe, entry_ts),
    )
    peak = rows[0]["peak"] if rows else None
    return float(peak) if peak is not None else None


def decide_exit(
    trade: Mapping[str, object],
    candle: Mapping[str, object],
    evaluation: scoring.Evaluation | None,
    exits_cfg: ExitConfig,
    peak_high: float | None,
) -> ExitResult | None:
    """First exit to fire for a long ``trade`` on this closed ``candle``, or None.

    Order is pessimistic (see module docstring): stop-loss → trailing stop →
    take-profit (intra-candle price exits, adverse first) → signal exit → time exit
    (close-time exits). ``peak_high`` is the favorable excursion since entry, used
    only for the trailing stop. ``evaluation`` is a fresh re-score of ``candle`` used
    only for the signal exit (None disables it).
    """
    entry_price = float(trade["entry_price"])
    stop_loss = float(trade["stop_loss"])
    take_profit = float(trade["take_profit"])
    entry_ts = int(trade["entry_ts"])  # type: ignore[arg-type]

    high = float(candle["high"])
    low = float(candle["low"])
    close = float(candle["close"])
    ts = int(candle["ts"])  # type: ignore[arg-type]

    # 1. Stop-loss — the deepest adverse level; assumed first within the candle.
    if low <= stop_loss:
        return ExitResult(STOP_LOSS, stop_loss, ts, MARKET)

    # 2. Trailing stop — rests one initial-stop-distance below the running peak,
    #    moving up but never down. Only a distinct exit once it has ratcheted above
    #    the fixed stop; below that the fixed stop-loss already covers the position.
    if exits_cfg.use_trailing_stop and peak_high is not None:
        stop_distance = entry_price - stop_loss
        trail_level = peak_high - stop_distance
        if trail_level > stop_loss and low <= trail_level:
            return ExitResult(TRAILING_STOP, trail_level, ts, MARKET)

    # 3. Take-profit — the favorable target, assumed after the adverse exits.
    if high >= take_profit:
        return ExitResult(TAKE_PROFIT, take_profit, ts, LIMIT)

    # 4. Signal exit — the conditions that justified entry no longer hold (FR-MX-4).
    #    Re-running scoring no longer fires the gate → close at the candle's close.
    if evaluation is not None and not evaluation.gate_passed:
        return ExitResult(SIGNAL_EXIT, close, ts, MARKET)

    # 5. Time exit — the hard cap, always active (FR-MX-5). Measured on candle time.
    if (ts - entry_ts) >= exits_cfg.max_hold_hours * SECONDS_PER_HOUR:
        return ExitResult(TIME_EXIT, close, ts, MARKET)

    return None


def close_trade(
    conn,
    client: ExchangeClient,
    trade: Mapping[str, object],
    exit_result: ExitResult,
) -> float:
    """Place the exit fill, compute P&L net of both fees, and journal the close.

    Returns the realized ``pnl``. The trade row is stamped closed (status, exit
    price/reason/time, pnl, pnl_pct, win) — the write that makes it learnable
    (PLAN §5.7, FR-MX-6).
    """
    symbol = str(trade["symbol"])
    size = float(trade["size"])
    entry_price = float(trade["entry_price"])
    exit_price = exit_result.exit_price

    # For a long, every exit is a SELL. Take-profit rests as a maker (limit); the
    # rest leave immediately as a taker (market). The fill carries its own fee.
    if exit_result.order_type == LIMIT:
        exit_fill = client.create_limit_order(symbol, SELL, size, exit_price)
    else:
        exit_fill = client.create_market_order(symbol, SELL, size, exit_price)

    # Reconstruct the entry fee from how the entry was placed (executor.open_trade
    # uses a maker order when prefer_maker is set), so pnl is net of BOTH fills
    # without persisting a fee column on the trade (FR-EX-3).
    entry_type = LIMIT if client.fees.prefer_maker else MARKET
    entry_fee = abs(entry_price * size) * client.fees.pct_for(entry_type)

    pnl = (exit_price - entry_price) * size - entry_fee - exit_fill.fee
    notional = entry_price * size
    pnl_pct = (pnl / notional * 100.0) if notional else 0.0
    win = 1 if pnl > 0 else 0

    db.update(
        conn,
        "trades",
        {
            "exit_ts": exit_result.exit_ts,
            "exit_price": exit_price,
            "exit_reason": exit_result.reason,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "status": "closed",
            "win": win,
        },
        "id = ?",
        (trade["id"],),
    )
    log.info(
        "[%s] CLOSE trade #%s %s via %s: exit %.2f | pnl %.2f (%.2f%%) %s (fees in=%.4f out=%.4f)",
        client.mode, trade["id"], symbol, exit_result.reason, exit_price,
        pnl, pnl_pct, "WIN" if win else "loss", entry_fee, exit_fill.fee,
    )
    return pnl


def monitor_trade(
    conn,
    client: ExchangeClient,
    trade: Mapping[str, object],
    scoring_cfg: scoring.ScoringConfig,
    exits_cfg: ExitConfig,
    timeframe: str,
) -> ExitResult | None:
    """Evaluate one open trade against the latest closed candle; close if any exit fires.

    Returns the ExitResult that fired, or None when the position is held for another
    cycle. Only candles strictly after the entry candle are considered (leakage-safe).
    """
    symbol = str(trade["symbol"])
    entry_ts = int(trade["entry_ts"])  # type: ignore[arg-type]

    candle = latest_candle(conn, symbol, timeframe)
    if candle is None or int(candle["ts"]) <= entry_ts:
        return None  # nothing new since entry — hold

    # Trailing reference: the peak since entry, never below the entry price.
    raw_peak = peak_high_since(conn, symbol, timeframe, entry_ts)
    peak_high = max(float(trade["entry_price"]), raw_peak) if raw_peak is not None else None

    # Re-score the latest candle for the signal (thesis-invalidation) exit.
    evaluation = scoring.evaluate(candle, scoring_cfg)

    exit_result = decide_exit(trade, candle, evaluation, exits_cfg, peak_high)
    if exit_result is None:
        return None
    close_trade(conn, client, trade, exit_result)
    return exit_result


def monitor_open_trades(
    conn,
    client: ExchangeClient,
    scoring_cfg: scoring.ScoringConfig,
    exits_cfg: ExitConfig,
    timeframe: str,
) -> int:
    """One monitor pass over every open trade in this mode; return how many closed.

    Called once per engine cycle after scoring/entry, completing the
    score → risk → execute → **monitor** loop (PLAN §1 runtime shape).
    """
    closed = 0
    for trade in open_trades(conn, client.mode):
        if monitor_trade(conn, client, trade, scoring_cfg, exits_cfg, timeframe) is not None:
            closed += 1
    return closed
