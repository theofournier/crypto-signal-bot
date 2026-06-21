"""Live on-chain collector (BUILD_PLAN Phase 9).

Pulls exchange inflow/outflow and large ("whale") transfer activity for an asset
and reduces it to a directional reading — **accumulation** (coins leaving
exchanges, typically bullish) vs **distribution** (coins moving onto exchanges,
typically bearish) — written to ``onchain_data`` (PLAN §5.1, §6).

Like every collector this is the base ``fetch -> normalize -> write`` shape and
obeys the two non-negotiables (PLAN §2):

  * **Observe, never decide** (FR-DC-5). It only writes rows; there is no
    scoring/trading logic here. Turning these rows into a sub-score happens later,
    in ``core/normalize.py`` (a separate Phase 9 task).
  * **Sources operate independently / degrade gracefully** (FR-DC-4, NFR-REL). An
    asset the source cannot cover, a missing API key, or a transient outage yields
    an empty cycle (nothing written) — never a crash, and never fabricated data.

**Pluggable source.** The actual data provider is injected (mirroring how
``market_collector`` injects its CCXT ``exchange``) so the collector is testable
without a network and the operator can swap providers without touching this loop.
The shipped default, :class:`EtherscanSource`, reads real ERC-20 transfer logs to
and from known centralized-exchange (CEX) hot wallets via Etherscan's free tier
(``ETHERSCAN_API_KEY`` in ``secrets.env``). The plan also names DefiLlama; it is a
fine alternative source — implement :class:`OnChainSource` and inject it.

**Honest coverage.** Real per-asset CEX flow without a paid analytics provider
(Glassnode/CryptoQuant/Nansen) is genuinely hard. The free Etherscan path only
covers **ERC-20 assets whose token contract and a set of CEX hot wallets are
known** (a small starter registry below, extend it). Native coins (BTC, native
ETH, SOL, …) live on other ledgers and are simply *not covered* here: the source
returns ``None`` for them and the collector writes nothing. Their on-chain
sub-score stays inactive/neutral — exactly the graceful degradation FR-DC-4 wants,
not a silent fake.

**Units.** A provider reports transfers in *token units*; the schema stores USD.
The collector converts using the latest ``market_data`` close for the asset (reuse
through the single DB, PLAN §2.2). If no price is stored yet (e.g. the market
collector has not run), the cycle is skipped rather than guessed — so the
on-chain collector naturally trails the market collector on a fresh DB.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from collectors.base_collector import BaseCollector
from data import db

# ── tunable defaults (overridable via config / constructor) ─────────
DEFAULT_POLL_SECONDS = 300        # slow: on-chain APIs rate-limit (PLAN §5.1)
DEFAULT_WINDOW_SECONDS = 3600     # aggregate flows over a trailing 1h window
DEFAULT_WHALE_USD = 1_000_000.0   # a transfer >= this USD counts as a whale move
DEFAULT_FLOW_DEADBAND = 0.05      # |net_flow| below this fraction of gross => neutral

# Directional readings stored in ``onchain_data.flow_signal``.
ACCUMULATION = "accumulation"  # net outflow from exchanges (bullish lean)
DISTRIBUTION = "distribution"  # net inflow to exchanges (bearish lean)
NEUTRAL = "neutral"            # inside the deadband — too close to call


@dataclass(frozen=True)
class Transfer:
    """One CEX transfer of an asset, in **token units** (USD is derived later).

    ``direction`` is ``"in"`` for tokens moving ONTO an exchange (distribution
    pressure) and ``"out"`` for tokens LEAVING an exchange (accumulation).
    """

    ts: int
    amount: float
    direction: str  # "in" | "out"


class OnChainSource(ABC):
    """A pluggable on-chain data provider. Implement and inject your own.

    The contract is deliberately tiny: given an asset and a time window, return
    the CEX transfers in that window, or ``None`` if this provider does not cover
    the asset (so the collector can degrade gracefully — FR-DC-4).
    """

    @abstractmethod
    def transfers(
        self, symbol: str, base_asset: str, since_ts: int, now_ts: int
    ) -> list[Transfer] | None:
        """CEX transfers for ``base_asset`` in ``[since_ts, now_ts]`` (token units).

        Returns ``None`` when the asset is not covered by this source (vs ``[]``
        which means "covered, but no transfers in the window").
        """


class OnChainCollector(BaseCollector):
    """Append one rolling on-chain observation per cycle for ONE asset.

    ``run_collectors.py`` runs one instance per configured pair so a failure on one
    asset never stalls another (FR-DC-4), exactly like the market collector.
    """

    table = "onchain_data"

    def __init__(
        self,
        conn,
        symbol: str,
        source: OnChainSource,
        timeframe: str = "1h",
        interval: float = DEFAULT_POLL_SECONDS,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        whale_usd: float = DEFAULT_WHALE_USD,
        flow_deadband: float = DEFAULT_FLOW_DEADBAND,
        now_fn=time.time,
    ) -> None:
        super().__init__(conn, interval, name=f"onchain:{symbol}")
        self.symbol = symbol
        self.base_asset = symbol.split("/")[0]  # "BTC/USDT" -> "BTC"
        self.source = source
        self.timeframe = timeframe
        self.window_seconds = window_seconds
        self.whale_usd = whale_usd
        self.flow_deadband = flow_deadband
        self._now = now_fn

    # ── fetch (talk to the source; no DB access) ───────────────────
    def fetch(self) -> list[Transfer] | None:
        """Pull CEX transfers for this asset over the trailing window.

        ``None`` means "this source does not cover the asset" — distinct from an
        empty list ("covered, no transfers"). Both result in nothing written, but
        only the former is logged as an uncovered asset.
        """
        now = int(self._now())
        return self.source.transfers(
            self.symbol, self.base_asset, now - self.window_seconds, now
        )

    # ── normalize (shape into a row; converts to USD via the DB) ────
    def normalize(self, raw: list[Transfer] | None) -> list[Mapping[str, Any]]:
        """Transfers -> one ``onchain_data`` row (USD flows + directional signal).

        Skips the cycle (writes nothing) when the asset is uncovered, or when no
        price is stored yet to convert token units to USD — both normal,
        non-error outcomes that keep the source independent (FR-DC-4).
        """
        if raw is None:
            self.log.debug("%s not covered by source; skipping", self.base_asset)
            return []

        price = self._latest_price_usd()
        if price is None:
            self.log.debug("no market price yet for %s; skipping on-chain cycle", self.symbol)
            return []

        reading = summarize_flows(raw, price, self.whale_usd, self.flow_deadband)
        reading["ts"] = int(self._now())
        reading["symbol"] = self.symbol
        return [reading]

    # ── helpers ─────────────────────────────────────────────────────
    def _latest_price_usd(self) -> float | None:
        """Most recent stored close for this asset (reuse through the DB).

        Used to convert token-unit transfers to the USD flows the schema stores.
        ``None`` on a fresh DB (the market collector simply has not run yet).
        """
        rows = db.query(
            self.conn,
            "SELECT close FROM market_data WHERE symbol = ? ORDER BY ts DESC LIMIT 1",
            (self.symbol,),
        )
        if not rows or rows[0]["close"] is None:
            return None
        return float(rows[0]["close"])


def summarize_flows(
    transfers: Sequence[Transfer],
    price_usd: float,
    whale_usd: float,
    deadband: float,
) -> dict[str, Any]:
    """Reduce raw transfers to USD flows + an accumulation/distribution reading.

    Pure (no I/O) so the directional logic is unit-testable without a network.

      * inflow  = USD moving ONTO exchanges  (``direction == "in"``)
      * outflow = USD LEAVING exchanges       (``direction == "out"``)
      * net_flow = outflow - inflow           (schema: "outflow − inflow")
      * whale_tx_count = transfers worth >= ``whale_usd``
      * flow_signal: accumulation if net outflow dominates, distribution if net
        inflow dominates, neutral inside a deadband (a fraction of gross flow) so
        balanced noise is not read as a direction.
    """
    inflow = outflow = 0.0
    whales = 0
    for t in transfers:
        usd = t.amount * price_usd
        if usd >= whale_usd:
            whales += 1
        if t.direction == "in":
            inflow += usd
        elif t.direction == "out":
            outflow += usd

    net_flow = outflow - inflow
    gross = inflow + outflow
    if gross > 0 and abs(net_flow) >= deadband * gross:
        signal = ACCUMULATION if net_flow > 0 else DISTRIBUTION
    else:
        signal = NEUTRAL

    return {
        "exchange_inflow": inflow,
        "exchange_outflow": outflow,
        "net_flow": net_flow,
        "whale_tx_count": whales,
        "flow_signal": signal,
    }


# ── default real source: Etherscan free tier (ERC-20 CEX flows) ─────

# Starter registries — extend for the ERC-20 assets you trade. Native-coin and
# non-Ethereum assets are intentionally absent (see module docstring).
TOKEN_CONTRACTS: dict[str, str] = {
    # base asset -> ERC-20 contract address (lowercased)
    "LINK": "0x514910771af9ca656af840dff83e8264ecf986ca",
    "UNI": "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984",
    "AAVE": "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9",
}

# A small set of well-known CEX hot wallets (lowercased). Real flow quality scales
# with how complete this list is; treat it as a seed the operator extends.
CEX_ADDRESSES: tuple[str, ...] = (
    "0x28c6c06298d514db089934071355e5743bf21d60",  # Binance 14
    "0x21a31ee1afc51d94c2efccaa2092ad1028285549",  # Binance 15
    "0xdfd5293d8e347dfe59e90efd55b2956a1343963d",  # Binance 16
    "0x56eddb7aa87536c09ccc2793473599fd21a8b17f",  # Binance 17
)

ETHERSCAN_BASE_URL = "https://api.etherscan.io/v2/api"  # unified multichain v2
ETHEREUM_CHAIN_ID = 1


class EtherscanSource(OnChainSource):
    """Real on-chain flows from Etherscan's free tier (ERC-20 only).

    For a supported ERC-20 asset it queries each known CEX hot wallet's token
    transfers (``action=tokentx``) and classifies each as inflow (``to`` a CEX) or
    outflow (``from`` a CEX). Unsupported assets return ``None`` so the collector
    degrades gracefully (FR-DC-4).

    ``urllib`` is imported at module load (stdlib, always available); the network
    call itself is the only non-pure part — parsing is factored into the pure
    :func:`parse_tokentx` so it stays unit-testable.
    """

    def __init__(
        self,
        api_key: str | None = None,
        token_contracts: Mapping[str, str] | None = None,
        cex_addresses: Sequence[str] | None = None,
        base_url: str = ETHERSCAN_BASE_URL,
        chain_id: int = ETHEREUM_CHAIN_ID,
        timeout: float = 15.0,
        max_transfers: int = 1000,
    ) -> None:
        # Key is optional at construction; absence is reported per-asset as
        # "source unavailable" rather than crashing the collector.
        self.api_key = api_key or os.environ.get("ETHERSCAN_API_KEY", "")
        self.token_contracts = {k.upper(): v.lower() for k, v in (token_contracts or TOKEN_CONTRACTS).items()}
        self.cex_addresses = tuple(a.lower() for a in (cex_addresses or CEX_ADDRESSES))
        self.base_url = base_url
        self.chain_id = chain_id
        self.timeout = timeout
        self.max_transfers = max_transfers
        self.log = logging.getLogger("collector.onchain.etherscan")

    def transfers(
        self, symbol: str, base_asset: str, since_ts: int, now_ts: int
    ) -> list[Transfer] | None:
        contract = self.token_contracts.get(base_asset.upper())
        if contract is None:
            return None  # not an ERC-20 we cover → uncovered asset
        if not self.api_key:
            self.log.warning("ETHERSCAN_API_KEY not set; on-chain source unavailable")
            return None

        out: list[Transfer] = []
        for addr in self.cex_addresses:
            try:
                payload = self._get_tokentx(addr, contract)
            except Exception:  # noqa: BLE001 — one address failing must not sink the rest
                self.log.exception("etherscan tokentx failed for %s; skipping address", addr)
                continue
            out.extend(parse_tokentx(payload, addr, since_ts))
        return out

    def _get_tokentx(self, address: str, contract: str) -> Mapping[str, Any]:
        """One Etherscan ``tokentx`` request for an (address, contract) pair."""
        params = {
            "chainid": self.chain_id,
            "module": "account",
            "action": "tokentx",
            "address": address,
            "contractaddress": contract,
            "page": 1,
            "offset": self.max_transfers,
            "sort": "desc",
            "apikey": self.api_key,
        }
        url = f"{self.base_url}?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(url, timeout=self.timeout) as resp:  # noqa: S310 - fixed https host
            return json.loads(resp.read().decode())


def parse_tokentx(
    payload: Mapping[str, Any], exchange_address: str, since_ts: int
) -> list[Transfer]:
    """Parse an Etherscan ``tokentx`` payload into in/out transfers (pure).

    Keeps only transfers at or after ``since_ts`` that touch ``exchange_address``:
    ``to == addr`` is an inflow (onto the exchange), ``from == addr`` an outflow.
    Token units are recovered from the raw integer value and ``tokenDecimal``.
    Malformed rows are skipped — free data is messy (PLAN §7, validate on intake).
    """
    if str(payload.get("status")) != "1" and not payload.get("result"):
        return []  # Etherscan signals "no transactions found" / errors via status

    addr = exchange_address.lower()
    transfers: list[Transfer] = []
    for row in payload.get("result", []):
        try:
            ts = int(row["timeStamp"])
            if ts < since_ts:
                continue
            decimals = int(row.get("tokenDecimal", 18))
            amount = int(row["value"]) / (10 ** decimals)
            frm = str(row["from"]).lower()
            to = str(row["to"]).lower()
        except (KeyError, ValueError, TypeError):
            continue  # skip malformed row rather than load garbage
        if to == addr:
            transfers.append(Transfer(ts=ts, amount=amount, direction="in"))
        elif frm == addr:
            transfers.append(Transfer(ts=ts, amount=amount, direction="out"))
    return transfers
