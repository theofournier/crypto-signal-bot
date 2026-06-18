"""CCXT wrapper + the dry-run switch (BUILD_PLAN Phase 5, PLAN §5.5/§5.6).

This is the ONLY module that may ever touch a real exchange, so the master safety
control lives here and nowhere else (FR-SF-1/2). One flag decides everything:

  * ``dry_run == True``  (the default) → every order is **simulated**: a fill is
    computed locally and logged; no network call is made, no real order is sent.
  * ``dry_run == False`` → the live path (Phase 11) forwards to CCXT.

The simulated and live paths are split so that, while ``dry_run`` is true, the live
branch is structurally unreachable — the release-blocking guarantee of FR-SF-2
("in simulation mode, no real order is sent under any circumstances").

**Fees on every fill (PLAN §5.6, FR-EX-3).** A simulated fill subtracts the
configured fee exactly as a real exchange would, otherwise dry-run and backtest
results are fiction. Limit (maker) orders pay the lower ``maker_pct``; market
(taker) orders pay ``taker_pct``. The realized cost of a fill is therefore the
notional plus (on a buy) or minus (on a sell) the fee.

This module places orders and reports fills only. It holds no scoring, risk, or
exit logic; it does not decide *whether* to trade (that is ``core/`` and
``execution/``). CCXT is imported lazily so dry-run and tests need no install.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

log = logging.getLogger("exchange")

# Order types and sides — the only vocabulary this module speaks.
LIMIT = "limit"
MARKET = "market"
BUY = "buy"
SELL = "sell"


@dataclass
class Fees:
    """Per-fill fee rates (PLAN §9 ``fees``). Fractions, e.g. 0.0010 == 0.10%."""

    maker_pct: float = 0.0010
    taker_pct: float = 0.0010
    prefer_maker: bool = True

    @classmethod
    def from_config(cls, cfg: dict) -> "Fees":
        fees = (cfg.get("fees") or {}) if cfg else {}
        return cls(
            maker_pct=float(fees.get("maker_pct", 0.0010)),
            taker_pct=float(fees.get("taker_pct", 0.0010)),
            prefer_maker=bool(fees.get("prefer_maker", True)),
        )

    def pct_for(self, order_type: str) -> float:
        """Maker fee for a resting limit order, taker fee for a market order."""
        return self.maker_pct if order_type == LIMIT else self.taker_pct

    def round_trip_pct(self) -> float:
        """Entry + exit fee for one trade, at the fee tier we prefer to trade at.

        Used by the risk gate's fee-aware edge filter (FR-RM-4): a setup whose
        expected move barely clears this is not worth taking.
        """
        side_pct = self.maker_pct if self.prefer_maker else self.taker_pct
        return 2.0 * side_pct


@dataclass
class Fill:
    """The result of one (simulated or real) order fill.

    ``fee`` is the absolute fee paid in quote currency. ``cash_flow`` is the signed
    effect on the account balance: a buy costs ``notional + fee`` (negative), a sell
    returns ``notional - fee`` (positive). P&L net of fees (FR-EX-3) is the sum of a
    trade's entry and exit cash flows.
    """

    symbol: str
    side: str
    order_type: str
    price: float
    size: float
    fee: float
    ts: int
    simulated: bool

    @property
    def notional(self) -> float:
        return self.price * self.size

    @property
    def cash_flow(self) -> float:
        if self.side == BUY:
            return -(self.notional + self.fee)
        return self.notional - self.fee


@dataclass
class ProtectiveOrder:
    """A stop-loss / take-profit order resting at the venue (PLAN §5.5).

    In dry-run it is a logged intent whose price is also persisted on the trade row,
    so the protection is recorded the moment the position opens (FR-EX-1). The
    monitor loop (Phase 6) is what later races these exits.
    """

    symbol: str
    side: str
    size: float
    price: float
    kind: str  # "stop_loss" | "take_profit"
    simulated: bool


class ExchangeClient:
    """Places orders through CCXT, or simulates them when ``dry_run`` is true.

    Construct with :meth:`from_config` so the dry-run flag and fees come straight
    from ``config.yaml``. The default is simulation (FR-SF-1): you must explicitly
    set ``dry_run: false`` to ever reach the live path.
    """

    def __init__(
        self,
        fees: Fees,
        dry_run: bool = True,
        exchange=None,
        now_fn=time.time,
    ) -> None:
        self.fees = fees
        self.dry_run = dry_run
        self._exchange = exchange
        self._now = now_fn

    @classmethod
    def from_config(cls, cfg: dict, exchange=None) -> "ExchangeClient":
        mode = (cfg.get("mode") or {}) if cfg else {}
        # Default to simulation if the flag is missing — never silently go live.
        dry_run = bool(mode.get("dry_run", True))
        return cls(fees=Fees.from_config(cfg), dry_run=dry_run, exchange=exchange)

    @property
    def mode(self) -> str:
        """The mode string stored on each trade row: ``dry`` or ``live``."""
        return "dry" if self.dry_run else "live"

    # ── orders ──────────────────────────────────────────────────────
    def create_limit_order(self, symbol: str, side: str, size: float, price: float) -> Fill:
        """Place a limit (maker) order — the lower-fee entry/exit (PLAN §5.6)."""
        return self._order(symbol, side, size, price, LIMIT)

    def create_market_order(self, symbol: str, side: str, size: float, price: float) -> Fill:
        """Place a market (taker) order. ``price`` is the reference fill price used
        to simulate the fill in dry-run; a live market order ignores it."""
        return self._order(symbol, side, size, price, MARKET)

    def set_stop_loss(self, symbol: str, side: str, size: float, price: float) -> ProtectiveOrder:
        """Rest a protective stop-loss at the venue (placed atomically at entry)."""
        return self._protective(symbol, side, size, price, "stop_loss")

    def set_take_profit(self, symbol: str, side: str, size: float, price: float) -> ProtectiveOrder:
        """Rest a protective take-profit at the venue (placed atomically at entry)."""
        return self._protective(symbol, side, size, price, "take_profit")

    # ── routing: simulate vs live ─────────────────────────────────────
    def _order(self, symbol: str, side: str, size: float, price: float, order_type: str) -> Fill:
        # The single fork between safe simulation and real money. While dry_run is
        # true the live branch below is never entered (FR-SF-2).
        if self.dry_run:
            return self._simulate_fill(symbol, side, size, price, order_type)
        return self._live_order(symbol, side, size, price, order_type)

    def _protective(self, symbol, side, size, price, kind) -> ProtectiveOrder:
        if self.dry_run:
            log.info(
                "[DRY] %s %s %s for %.8f @ %.2f (no real order placed)",
                kind, side, symbol, size, price,
            )
            return ProtectiveOrder(symbol, side, size, price, kind, simulated=True)
        return self._live_protective(symbol, side, size, price, kind)

    # ── dry-run simulation (Phase 5) ──────────────────────────────────
    def _simulate_fill(self, symbol, side, size, price, order_type) -> Fill:
        """Compute a local fill with the fee deducted, exactly as a venue would.

        The fee is ``price * size * fee_pct`` (PLAN §5.6). Nothing is sent over the
        network; the fill is logged so simulation activity is auditable (FR-SF-4).
        """
        fee = abs(price * size) * self.fees.pct_for(order_type)
        fill = Fill(
            symbol=symbol,
            side=side,
            order_type=order_type,
            price=price,
            size=size,
            fee=fee,
            ts=int(self._now()),
            simulated=True,
        )
        log.info(
            "[DRY] simulated %s %s %s: %.8f @ %.2f | notional=%.2f fee=%.4f cash_flow=%.2f",
            order_type, side, symbol, size, price, fill.notional, fee, fill.cash_flow,
        )
        return fill

    # ── live path (Phase 11 — guarded, unreachable while dry_run) ─────
    def _live_order(self, symbol, side, size, price, order_type) -> Fill:
        """Forward a real order to CCXT. Reached only when ``dry_run`` is False.

        Phase 5 is dry-run only; this exists so the seam is explicit, but it refuses
        to run while in simulation and requires an exchange to be wired in. A real
        fill price/fee would be read back from the exchange response in Phase 11.
        """
        if self.dry_run:  # defensive: must never reach a live order in simulation
            raise RuntimeError("refusing to place a live order while dry_run is True")
        if self._exchange is None:
            raise RuntimeError("live mode requires a configured exchange (see Phase 11)")
        if order_type == LIMIT:
            self._exchange.create_limit_order(symbol, side, size, price)
        else:
            self._exchange.create_market_order(symbol, side)
        fee = abs(price * size) * self.fees.pct_for(order_type)
        return Fill(symbol, side, order_type, price, size, fee, int(self._now()), simulated=False)

    def _live_protective(self, symbol, side, size, price, kind) -> ProtectiveOrder:
        if self.dry_run:
            raise RuntimeError("refusing to place a live protective order while dry_run is True")
        if self._exchange is None:
            raise RuntimeError("live mode requires a configured exchange (see Phase 11)")
        # Concrete stop/limit exit wiring is Phase 11; the seam is defined here.
        raise NotImplementedError("live protective orders are implemented in Phase 11")
