"""The risk gate: veto checks, then sizing (BUILD_PLAN Phase 5, PLAN §5.4).

A gated signal is not yet a trade. It must first clear the risk gate, whose job is
to ensure no single trade can do outsized damage (US-4). Blocking is a valid,
logged outcome — not an error (FR-RM-5). The gate runs its vetoes in order and,
only if all pass, sizes the position:

  1. **Exposure** (FR-RM-1)   — too many open positions already? → block.
  2. **Drawdown** (FR-RM-1)   — in a drawdown past the pause level? → block.
  3. **Volatility** (FR-RM-2) — market in a chaotic spike? → sit out.
  4. **Fee-aware edge** (FR-RM-4) — does the expected move clear the round-trip fee
     by the configured margin? If not, the fee eats the edge → block.
  5. **Sizing** (FR-RM-3)     — fractional Kelly from the operator's measured edge
     and capital, never a fixed arbitrary amount.

Fractional Kelly (PLAN §5.4):

    edge_fraction = win_rate - (1 - win_rate) / reward_risk_ratio
    notional = current_equity * kelly_fraction * max(edge_fraction, 0)

The stake is a fraction of **current equity** — the starting bankroll plus realized
P&L reconstructed from the journal — so it **compounds after wins and de-risks after
losses** rather than betting forever off a static number. The notional is then capped
by the cash actually free to deploy: equity not already tied up in open positions,
under a configured exposure ceiling. For spot long-or-flat you can never deploy more
cash than you hold (FR-RM-1/3).

``win_rate`` and ``reward_risk_ratio`` are read from the ``trades`` journal so the
gate self-calibrates as evidence accumulates. Until there are enough closed trades
to trust (FR-LE-4: ≥ 100), it falls back to the configured starting assumptions —
small samples are noise and must not drive sizing.

Equity is journal-derived (no separate mutable balance row, so it stays auditable
and reconstructable — FR-DP-3). In dry-run the starting balance is ``risk.bankroll``;
Phase 11 (live) swaps that source for the exchange's real quote-currency balance,
leaving everything downstream unchanged.

This module reads the DB (journal + recent market rows) and returns a decision; it
never places an order. Execution is ``execution/executor.py``.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass

from data import db

log = logging.getLogger("risk")

# Below this many closed trades, journal stats are noise — use config defaults
# instead of self-calibrating (FR-LE-4: a verdict needs ≥ 100 closed trades).
MIN_TRADES_FOR_JOURNAL_STATS = 100
# How many recent candles define "normal" volatility for the spike filter.
VOLATILITY_LOOKBACK = 50
# Need at least this much history before the volatility filter can judge a spike.
VOLATILITY_MIN_SAMPLES = 10


@dataclass
class RiskConfig:
    """The ``risk`` knobs from ``config.yaml`` (PLAN §9), with safe defaults."""

    kelly_fraction: float = 0.25
    max_open_positions: int = 3
    max_drawdown_pause: float = 0.15
    volatility_filter: bool = True
    min_edge_over_fees: float = 1.5
    default_win_rate: float = 0.50
    default_reward_risk: float = 2.0
    bankroll: float = 10_000.0  # STARTING equity; current equity = this + realized P&L
    volatility_spike_mult: float = 2.5
    # Ceiling on total capital deployed across all open positions, as a fraction of
    # current equity. 1.0 = fully investable (available cash is the natural cap for
    # spot); set < 1.0 to always hold dry powder.
    max_exposure_fraction: float = 1.0

    @classmethod
    def from_config(cls, cfg: dict) -> "RiskConfig":
        risk = (cfg.get("risk") or {}) if cfg else {}
        return cls(
            kelly_fraction=float(risk.get("kelly_fraction", 0.25)),
            max_open_positions=int(risk.get("max_open_positions", 3)),
            max_drawdown_pause=float(risk.get("max_drawdown_pause", 0.15)),
            volatility_filter=bool(risk.get("volatility_filter", True)),
            min_edge_over_fees=float(risk.get("min_edge_over_fees", 1.5)),
            default_win_rate=float(risk.get("default_win_rate", 0.50)),
            default_reward_risk=float(risk.get("default_reward_risk", 2.0)),
            bankroll=float(risk.get("bankroll", 10_000.0)),
            volatility_spike_mult=float(risk.get("volatility_spike_mult", 2.5)),
            max_exposure_fraction=float(risk.get("max_exposure_fraction", 1.0)),
        )


@dataclass
class RiskDecision:
    """The gate's verdict. ``size`` is in base units (0 when blocked).

    ``reason`` is human-readable so a blocked trade is auditable (FR-RM-5, G3).
    ``win_rate``/``reward_risk`` record the inputs that produced the size.
    """

    approved: bool
    size: float
    reason: str
    win_rate: float
    reward_risk: float


class RiskGate:
    """Runs the veto checks and fractional-Kelly sizing for one mode (dry/live).

    Bound to a DB connection and the risk config; ``assess`` is called per candidate
    trade. Stats are read only from trades of the same ``mode`` so a dry-run journal
    never sizes a live trade and vice versa.
    """

    def __init__(self, conn, cfg: RiskConfig, mode: str = "dry") -> None:
        self.conn = conn
        self.cfg = cfg
        self.mode = mode

    def assess(
        self,
        symbol: str,
        timeframe: str,
        entry_price: float,
        take_profit: float,
        round_trip_fee_pct: float,
        bb_width: float | None = None,
    ) -> RiskDecision:
        """Decide whether (and how large) to take a candidate long entry.

        ``take_profit`` defines the expected favorable move used by the fee filter;
        ``round_trip_fee_pct`` is the trade's entry+exit fee (from ``exchange``).
        """
        win_rate, reward_risk = self._journal_stats()

        # 1. Exposure — already holding the configured maximum?
        open_count = self._open_position_count()
        if open_count >= self.cfg.max_open_positions:
            return self._block(
                f"exposure: {open_count} open >= max {self.cfg.max_open_positions}",
                win_rate, reward_risk,
            )

        # 2. Drawdown — paused after a peak-to-trough decline past the limit?
        drawdown = self._current_drawdown()
        if drawdown >= self.cfg.max_drawdown_pause:
            return self._block(
                f"drawdown {drawdown:.1%} >= pause {self.cfg.max_drawdown_pause:.1%}",
                win_rate, reward_risk,
            )

        # 3. Volatility — sit out a chaotic spike.
        if self.cfg.volatility_filter and self._is_volatility_spike(symbol, timeframe, bb_width):
            return self._block(
                f"volatility spike: bb_width {bb_width:.4f} > "
                f"{self.cfg.volatility_spike_mult}x recent median",
                win_rate, reward_risk,
            )

        # 4. Fee-aware edge filter (FR-RM-4) — does the move clear the fees?
        expected_move = (take_profit - entry_price) / entry_price if entry_price else 0.0
        min_move = self.cfg.min_edge_over_fees * round_trip_fee_pct
        if expected_move < min_move:
            return self._block(
                f"fee filter: expected move {expected_move:.4%} < "
                f"{self.cfg.min_edge_over_fees}x round-trip fee ({min_move:.4%})",
                win_rate, reward_risk,
            )

        # 5. Sizing — fractional Kelly on CURRENT equity, capped by available cash.
        edge_fraction = win_rate - (1.0 - win_rate) / reward_risk
        if edge_fraction <= 0.0:
            return self._block(
                f"no positive edge: win_rate {win_rate:.2f} / RR {reward_risk:.2f} "
                f"=> edge {edge_fraction:.3f}",
                win_rate, reward_risk,
            )

        # Bet a fraction of current equity (bankroll + realized P&L) so the stake
        # compounds after wins and de-risks after losses (FR-RM-3), then cap it by the
        # cash actually free to deploy — equity not already tied up in open positions,
        # under the exposure ceiling. You can never deploy more cash than you hold.
        equity = self._current_equity()
        if equity <= 0.0:
            return self._block(f"account equity depleted ({equity:.2f})", win_rate, reward_risk)

        kelly_notional = equity * self.cfg.kelly_fraction * edge_fraction
        deployed = self._deployed_capital()
        free_cash = min(equity * self.cfg.max_exposure_fraction, equity) - deployed
        if free_cash <= 0.0:
            return self._block(
                f"no capital free: deployed {deployed:.2f} of equity {equity:.2f} "
                f"(exposure ceiling {self.cfg.max_exposure_fraction:.0%})",
                win_rate, reward_risk,
            )
        notional = min(kelly_notional, free_cash)
        size = notional / entry_price if entry_price else 0.0
        if size <= 0.0:
            return self._block("computed size is zero", win_rate, reward_risk)

        capped = " (capped by free cash)" if notional < kelly_notional else ""
        reason = (
            f"approved: size {size:.8f} (notional {notional:.2f}{capped}); "
            f"Kelly {self.cfg.kelly_fraction} x edge {edge_fraction:.3f} on equity "
            f"{equity:.2f} (win_rate {win_rate:.2f}, RR {reward_risk:.2f}); "
            f"deployed {deployed:.2f}, free {free_cash:.2f}; "
            f"expected move {expected_move:.4%} >= {min_move:.4%}; "
            f"open {open_count}/{self.cfg.max_open_positions}, drawdown {drawdown:.1%}"
        )
        log.info("%s: %s", symbol, reason)
        return RiskDecision(True, size, reason, win_rate, reward_risk)

    # ── checks ────────────────────────────────────────────────────────
    def _open_position_count(self) -> int:
        rows = db.query(
            self.conn,
            "SELECT COUNT(*) AS n FROM trades WHERE status = 'open' AND mode = ?",
            (self.mode,),
        )
        return int(rows[0]["n"]) if rows else 0

    # ── account equity & capital deployment ────────────────────────────
    def _realized_pnl(self) -> float:
        """Net realized P&L (already fee-inclusive) over closed trades, this mode."""
        rows = db.query(
            self.conn,
            "SELECT COALESCE(SUM(pnl), 0.0) AS total FROM trades "
            "WHERE status = 'closed' AND mode = ? AND pnl IS NOT NULL",
            (self.mode,),
        )
        return float(rows[0]["total"]) if rows else 0.0

    def _current_equity(self) -> float:
        """Equity backing position sizing: starting bankroll + realized P&L.

        Reconstructed from the journal so it compounds wins and shrinks after losses
        with no extra mutable state (matches ``_current_drawdown``'s equity curve and
        the postmortem's, so sizing, drawdown, and the report all agree). Phase 11
        swaps the bankroll term for the exchange's live balance; nothing else changes.
        """
        return self.cfg.bankroll + self._realized_pnl()

    def _deployed_capital(self) -> float:
        """Cash tied up in currently-open positions (entry notional), this mode."""
        rows = db.query(
            self.conn,
            "SELECT COALESCE(SUM(entry_price * size), 0.0) AS total FROM trades "
            "WHERE status = 'open' AND mode = ?",
            (self.mode,),
        )
        return float(rows[0]["total"]) if rows else 0.0

    def _current_drawdown(self) -> float:
        """Peak-to-trough decline of realized equity, as a fraction of the peak.

        Equity starts at ``bankroll`` and moves by each closed trade's net P&L (which
        is already fee-inclusive). Returns the drawdown at the latest point; 0 when
        there is no journal yet.
        """
        rows = db.query(
            self.conn,
            "SELECT pnl FROM trades WHERE status = 'closed' AND mode = ? "
            "AND pnl IS NOT NULL ORDER BY exit_ts, id",
            (self.mode,),
        )
        equity = self.cfg.bankroll
        peak = equity
        for row in rows:
            equity += float(row["pnl"])
            peak = max(peak, equity)
        if peak <= 0:
            return 0.0
        return max((peak - equity) / peak, 0.0)

    def _is_volatility_spike(self, symbol: str, timeframe: str, bb_width: float | None) -> bool:
        """True when current ``bb_width`` exceeds ``mult`` x its recent median.

        A relative, asset-agnostic measure of a "chaotic spike" (PLAN §5.4). Returns
        False when we lack a current reading or enough history to judge.
        """
        if bb_width is None:
            return False
        rows = db.query(
            self.conn,
            "SELECT bb_width FROM market_data WHERE symbol = ? AND timeframe = ? "
            "AND bb_width IS NOT NULL ORDER BY ts DESC LIMIT ?",
            (symbol, timeframe, VOLATILITY_LOOKBACK),
        )
        recent = [float(r["bb_width"]) for r in rows]
        if len(recent) < VOLATILITY_MIN_SAMPLES:
            return False
        median = statistics.median(recent)
        return median > 0 and bb_width > self.cfg.volatility_spike_mult * median

    # ── self-calibration from the journal ──────────────────────────────
    def _journal_stats(self) -> tuple[float, float]:
        """(win_rate, reward_risk) from closed trades, or config defaults.

        Below ``MIN_TRADES_FOR_JOURNAL_STATS`` closed trades the sample is noise
        (FR-LE-4), so the configured starting assumptions are used instead.
        """
        rows = db.query(
            self.conn,
            "SELECT pnl FROM trades WHERE status = 'closed' AND mode = ? "
            "AND pnl IS NOT NULL",
            (self.mode,),
        )
        pnls = [float(r["pnl"]) for r in rows]
        if len(pnls) < MIN_TRADES_FOR_JOURNAL_STATS:
            return self.cfg.default_win_rate, self.cfg.default_reward_risk

        wins = [p for p in pnls if p > 0]
        losses = [-p for p in pnls if p <= 0]
        win_rate = len(wins) / len(pnls)
        avg_win = statistics.fmean(wins) if wins else 0.0
        avg_loss = statistics.fmean(losses) if losses else 0.0
        if avg_loss <= 0:
            reward_risk = self.cfg.default_reward_risk
        else:
            reward_risk = avg_win / avg_loss
        return win_rate, reward_risk

    def _block(self, reason: str, win_rate: float, reward_risk: float) -> RiskDecision:
        log.info("blocked: %s", reason)  # blocking is a valid, logged outcome (FR-RM-5)
        return RiskDecision(False, 0.0, reason, win_rate, reward_risk)
