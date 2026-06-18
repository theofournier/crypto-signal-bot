"""Phase 5 tests for the exchange client (exchange/client.py).

The release-blocking guarantee (FR-SF-2): while ``dry_run`` is true, NO real order
is ever sent. These tests prove fills are simulated locally, fees are deducted on
every fill exactly as a venue would (FR-EX-3), maker/taker rates are applied per
order type, and the live path is unreachable in simulation.

Run with: pytest tests/test_exchange.py
"""

from __future__ import annotations

import pytest

from exchange.client import BUY, SELL, ExchangeClient, Fees


class ExplodingExchange:
    """A stand-in venue that fails the test if any order method is called.

    Wired into a dry-run client to prove the simulation path never touches it
    (FR-SF-2: no real order under any circumstances while dry_run is true).
    """

    def create_limit_order(self, *a, **k):  # pragma: no cover - must never run
        raise AssertionError("real limit order placed during dry-run!")

    def create_market_order(self, *a, **k):  # pragma: no cover - must never run
        raise AssertionError("real market order placed during dry-run!")


def client(dry_run=True, exchange=None, **fee_kw):
    fees = Fees(maker_pct=fee_kw.get("maker_pct", 0.0010),
                taker_pct=fee_kw.get("taker_pct", 0.0020),
                prefer_maker=fee_kw.get("prefer_maker", True))
    return ExchangeClient(fees=fees, dry_run=dry_run, exchange=exchange, now_fn=lambda: 1_700_000_000)


# ── simulation: no real order, fees deducted ─────────────────────
def test_dry_run_simulates_limit_fill_and_never_touches_exchange():
    c = client(exchange=ExplodingExchange())
    fill = c.create_limit_order("BTC/USDT", BUY, 0.5, 100.0)
    assert fill.simulated is True
    assert fill.price == 100.0 and fill.size == 0.5
    # maker fee on a limit order: 100 * 0.5 * 0.0010 = 0.05
    assert fill.fee == pytest.approx(0.05)


def test_market_order_uses_taker_fee():
    c = client()
    fill = c.create_market_order("BTC/USDT", BUY, 0.5, 100.0)
    # taker fee: 100 * 0.5 * 0.0020 = 0.10 (higher than maker)
    assert fill.order_type == "market"
    assert fill.fee == pytest.approx(0.10)


def test_buy_cash_flow_pays_notional_plus_fee():
    c = client()
    fill = c.create_limit_order("BTC/USDT", BUY, 0.5, 100.0)
    assert fill.notional == pytest.approx(50.0)
    assert fill.cash_flow == pytest.approx(-(50.0 + 0.05))  # money leaves the account


def test_sell_cash_flow_returns_notional_minus_fee():
    c = client()
    fill = c.create_limit_order("BTC/USDT", SELL, 0.5, 100.0)
    assert fill.cash_flow == pytest.approx(50.0 - 0.05)  # money comes in, net of fee


def test_protective_orders_are_simulated_in_dry_run():
    c = client(exchange=ExplodingExchange())
    sl = c.set_stop_loss("BTC/USDT", SELL, 0.5, 95.0)
    tp = c.set_take_profit("BTC/USDT", SELL, 0.5, 110.0)
    assert sl.simulated and sl.kind == "stop_loss" and sl.price == 95.0
    assert tp.simulated and tp.kind == "take_profit" and tp.price == 110.0


# ── mode + the live guard ─────────────────────────────────────────
def test_mode_string_reflects_dry_run_flag():
    assert client(dry_run=True).mode == "dry"
    assert client(dry_run=False).mode == "live"


def test_default_mode_is_simulation_from_config():
    # config without a mode block must default to dry-run (FR-SF-1: sim is default).
    c = ExchangeClient.from_config({})
    assert c.dry_run is True and c.mode == "dry"


def test_round_trip_fee_uses_preferred_side():
    c = client(maker_pct=0.0010, taker_pct=0.0020, prefer_maker=True)
    assert c.fees.round_trip_pct() == pytest.approx(0.0020)  # 2 x maker


def test_live_path_requires_an_exchange_and_is_off_in_dry_run():
    # A live client with no exchange wired in refuses to place an order.
    live = client(dry_run=False, exchange=None)
    with pytest.raises(RuntimeError):
        live.create_limit_order("BTC/USDT", BUY, 0.5, 100.0)
