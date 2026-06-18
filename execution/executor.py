"""Atomic entry + protective exits, then journal the trade (BUILD_PLAN Phase 5).

When a signal clears the gate (``core/scoring``) and the risk gate (``core/risk``),
this module opens the position. The cardinal rule is **atomicity** (FR-EX-1, PLAN
§5.5): the stop-loss and take-profit go in *with* the entry, never after — there is
no observable window in which the position exists unprotected. The full set of exit
levels is defined here at entry; racing them to a close is the monitor's job
(Phase 6), but the protection is in place from the first instant.

Entry uses a **limit (maker) order** when ``fees.prefer_maker`` is set, to pay the
lower fee (FR-EX-2, PLAN §5.6). A limit entry may not fill in live trading, which is
acceptable; in dry-run the simulator fills it at the limit price.

Initial stop distance follows ``exits.stop_method`` (PLAN §9):
  * ``fixed_pct`` — a flat percentage of entry price.
  * ``atr``       — ``atr_multiplier`` x ATR(14) over recent candles (volatility
    based), falling back to ``fixed_pct`` when ATR cannot be computed yet.
Take-profit sits at ``reward_risk_ratio`` x the stop distance above entry.

v1 is long-or-flat (FR-EX-4): entries are always ``buy``; the protective exits are
``sell`` orders. Nothing here ever opens a short. The trade is written to the
``trades`` journal with ``status='open'`` and the client's ``mode`` (dry/live), the
shared output that the learning loop later reads (FR-DP-1/2).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Mapping, Sequence

from data import db
from exchange.client import BUY, SELL, ExchangeClient

log = logging.getLogger("executor")

# Period for the ATR-based stop. Standard 14.
ATR_PERIOD = 14


@dataclass
class ExitConfig:
    """The ``exits`` knobs from ``config.yaml`` (PLAN §9), with safe defaults."""

    reward_risk_ratio: float = 2.0
    stop_method: str = "atr"
    stop_fixed_pct: float = 0.03
    atr_multiplier: float = 2.0
    use_trailing_stop: bool = True
    max_hold_hours: float = 48.0

    @classmethod
    def from_config(cls, cfg: dict) -> "ExitConfig":
        exits = (cfg.get("exits") or {}) if cfg else {}
        return cls(
            reward_risk_ratio=float(exits.get("reward_risk_ratio", 2.0)),
            stop_method=str(exits.get("stop_method", "atr")),
            stop_fixed_pct=float(exits.get("stop_fixed_pct", 0.03)),
            atr_multiplier=float(exits.get("atr_multiplier", 2.0)),
            use_trailing_stop=bool(exits.get("use_trailing_stop", True)),
            max_hold_hours=float(exits.get("max_hold_hours", 48.0)),
        )


def _atr(candles: Sequence[Mapping[str, object]], period: int = ATR_PERIOD) -> float | None:
    """Average True Range over the most recent ``period`` candles, or None.

    ``candles`` are ``market_data`` rows oldest→newest. True range uses the prior
    close, so at least ``period + 1`` candles with OHLC are required.
    """
    rows = [c for c in candles if c.get("high") is not None and c.get("low") is not None]
    if len(rows) < period + 1:
        return None
    trs: list[float] = []
    for prev, cur in zip(rows[-period - 1 :], rows[-period:]):
        high = float(cur["high"])
        low = float(cur["low"])
        prev_close = float(prev["close"])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(trs) / len(trs) if trs else None


def compute_exits(
    entry_price: float,
    candles: Sequence[Mapping[str, object]],
    exits_cfg: ExitConfig,
) -> tuple[float, float]:
    """Initial (stop_loss, take_profit) for a long entry (PLAN §5.5).

    Stop distance per ``stop_method``; take-profit at ``reward_risk_ratio`` x that
    distance above entry. ATR falls back to fixed-percent when it cannot be computed
    yet, so an entry is never left without a protective level.
    """
    if exits_cfg.stop_method == "atr":
        atr = _atr(candles)
        stop_distance = atr * exits_cfg.atr_multiplier if atr is not None else None
        if stop_distance is None or stop_distance <= 0:
            stop_distance = entry_price * exits_cfg.stop_fixed_pct
    else:
        stop_distance = entry_price * exits_cfg.stop_fixed_pct

    stop_loss = entry_price - stop_distance
    take_profit = entry_price + stop_distance * exits_cfg.reward_risk_ratio
    return stop_loss, take_profit


def open_trade(
    conn,
    client: ExchangeClient,
    signal_id: int,
    symbol: str,
    entry_price: float,
    size: float,
    stop_loss: float,
    take_profit: float,
    now_fn=time.time,
) -> int:
    """Place entry + SL + TP atomically and journal the open trade (FR-EX-1).

    Order matters: the entry fills, then both protective orders are placed before
    control returns and before any other system cycle — the position is never left
    unprotected. Returns the new ``trades`` row id.
    """
    # Entry: prefer the lower-fee maker (limit) order where configured (FR-EX-2).
    if client.fees.prefer_maker:
        entry = client.create_limit_order(symbol, BUY, size, entry_price)
    else:
        entry = client.create_market_order(symbol, BUY, size, entry_price)

    # Protection in immediately — for a long, the exits are sells (PLAN §5.5).
    client.set_stop_loss(symbol, SELL, size, stop_loss)
    client.set_take_profit(symbol, SELL, size, take_profit)

    row = {
        "signal_id": signal_id,
        "symbol": symbol,
        "direction": "long",  # v1 long-or-flat (FR-EX-4)
        "mode": client.mode,
        "entry_ts": int(now_fn()),
        "entry_price": entry.price,
        "size": size,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "status": "open",
    }
    trade_id = db.insert(conn, "trades", row)
    log.info(
        "[%s] OPEN trade #%d %s: %.8f @ %.2f | SL %.2f TP %.2f (signal #%d)",
        client.mode, trade_id, symbol, size, entry.price, stop_loss, take_profit, signal_id,
    )
    return trade_id


def has_open_position(conn, symbol: str, mode: str) -> bool:
    """True if there is already an open trade for this pair in this mode.

    Guards against re-entering a symbol we already hold (v1 is long-or-flat: one
    position per pair) and against opening twice on a poll re-run.
    """
    rows = db.query(
        conn,
        "SELECT 1 FROM trades WHERE symbol = ? AND mode = ? AND status = 'open' LIMIT 1",
        (symbol, mode),
    )
    return bool(rows)


def try_open_from_signal(
    conn,
    client: ExchangeClient,
    risk_gate,
    exits_cfg: ExitConfig,
    signal_id: int,
    signal_row: Mapping[str, object],
    market_row: Mapping[str, object],
    recent_candles: Sequence[Mapping[str, object]],
    now_fn=time.time,
) -> int | None:
    """Turn a *gated* signal into a simulated open trade, if risk approves.

    The full Phase 5 path: gate check → no duplicate position → compute exits →
    risk gate (sizing + vetoes) → atomic entry. Returns the new trade id, or None
    when nothing was opened (not firing, already holding, or risk blocked). Risk
    blocks are logged inside the gate (FR-RM-5); they are a normal outcome.
    """
    if not signal_row.get("gate_passed") or signal_row.get("direction") != "long":
        return None
    if has_open_position(conn, str(market_row["symbol"]), client.mode):
        log.debug("%s: already holding an open position — skipping entry", market_row["symbol"])
        return None

    symbol = str(market_row["symbol"])
    timeframe = str(market_row["timeframe"])
    entry_price = float(market_row["close"])
    bb_width = market_row["bb_width"]
    bb_width = float(bb_width) if bb_width is not None else None

    stop_loss, take_profit = compute_exits(entry_price, recent_candles, exits_cfg)

    decision = risk_gate.assess(
        symbol=symbol,
        timeframe=timeframe,
        entry_price=entry_price,
        take_profit=take_profit,
        round_trip_fee_pct=client.fees.round_trip_pct(),
        bb_width=bb_width,
    )
    if not decision.approved:
        return None

    return open_trade(
        conn, client, signal_id, symbol, entry_price, decision.size,
        stop_loss, take_profit, now_fn=now_fn,
    )
