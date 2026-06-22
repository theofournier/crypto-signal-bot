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

**Pluggable, composable sources.** The data provider is injected (mirroring how
``market_collector`` injects its CCXT ``exchange``) so the collector is testable
without a network and providers swap without touching this loop. Three real sources
ship, and they are **complementary** (combined via :class:`CompositeSource`, first
to cover an asset wins):

  * :class:`EtherscanSource` — real CEX inflow/outflow + whale transfers for
    **ERC-20 tokens** on any EVM chain, via Etherscan's free multichain v2 API
    (one ``ETHERSCAN_API_KEY`` in ``secrets.env`` covers every chain by ``chainid``).
  * :class:`BitcoinEsploraSource` — real CEX inflow/outflow + whale transfers for
    **native BTC**, via the free, keyless Esplora API (blockstream.info). Same
    true-flow quality class as Etherscan, for the headline asset neither other
    source reads well.
  * :class:`DefiLlamaSource` — **chain TVL momentum** (rising = accumulation,
    falling = distribution) for native L1/L2 coins (SOL, AVAX, ETH, …), free and
    keyless. This covers exactly the native coins the flow sources cannot.

**Config-driven, honest coverage.** Real per-asset CEX flow without a paid
analytics provider (Glassnode/CryptoQuant/Nansen) is genuinely hard, so coverage
is partial and explicit. Both sources read their registries from config
(``onchain.etherscan.chains`` / ``onchain.defillama.chains``) — add tokens/chains
there with **no code change**; built-in starter registries are used when config
omits them. An asset no source covers returns ``None`` and the collector writes
nothing, leaving its on-chain sub-score inactive/neutral — the graceful
degradation FR-DC-4 wants, not a silent fake. Each source fills only the columns
it actually measures (a TVL source leaves the CEX-flow columns ``NULL``).

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

    A source answers two questions: does it **cover** an asset, and (if so) what is
    the asset's on-chain **reading** over a window? The reading is the directional
    essence the engine cares about — different sources measure it differently (CEX
    transfer flow vs. ecosystem TVL momentum), so each returns the row payload
    directly rather than a single raw shape. ``None`` from ``read`` means "no data
    this cycle" (uncovered, or a transient gap) — the collector then writes nothing,
    degrading gracefully (FR-DC-4).
    """

    @abstractmethod
    def covers(self, base_asset: str) -> bool:
        """True if this source can produce a reading for ``base_asset``."""

    @abstractmethod
    def read(
        self,
        base_asset: str,
        since_ts: int,
        now_ts: int,
        *,
        price_usd: float | None,
        whale_usd: float,
        deadband: float,
    ) -> dict | None:
        """The on-chain reading for ``base_asset`` (the ``onchain_data`` flow
        columns) over ``[since_ts, now_ts]``, or ``None`` if unavailable.

        ``price_usd`` (latest market close, or ``None``) lets token-unit sources
        convert to USD; sources already denominated in USD ignore it. ``whale_usd``
        and ``deadband`` are the reduction knobs each source applies as relevant.
        """


class TransferSource(OnChainSource):
    """Base for sources that observe **CEX transfers** (Etherscan, future explorers).

    Subclasses implement :meth:`transfers` (raw, token-unit transfers to/from CEX
    wallets); this base reduces them to USD flows via :func:`summarize_flows`. A
    token-unit source needs a price to convert, so ``read`` skips the cycle when
    ``price_usd`` is missing — a normal, non-error outcome (FR-DC-4).
    """

    @abstractmethod
    def transfers(
        self, base_asset: str, since_ts: int, now_ts: int
    ) -> list[Transfer] | None:
        """CEX transfers for ``base_asset`` in ``[since_ts, now_ts]`` (token units).

        Returns ``None`` when the asset is not covered (vs ``[]`` = "covered, no
        transfers in the window").
        """

    def read(
        self,
        base_asset: str,
        since_ts: int,
        now_ts: int,
        *,
        price_usd: float | None,
        whale_usd: float,
        deadband: float,
    ) -> dict | None:
        txs = self.transfers(base_asset, since_ts, now_ts)
        if txs is None or price_usd is None:
            return None  # uncovered, or no price yet to convert token units to USD
        return summarize_flows(txs, price_usd, whale_usd, deadband)


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

    # ── fetch (ask the source for a reading) ───────────────────────
    def fetch(self) -> dict | None:
        """Get this asset's on-chain reading over the trailing window, or ``None``.

        Resolves the latest price from the DB (so token-unit sources can convert to
        USD — reuse through the single source of truth, PLAN §2.2) and hands it to
        the source. ``None`` when the asset is covered by no source, or a source has
        no data this cycle — both mean "write nothing" (FR-DC-4).
        """
        if not self.source.covers(self.base_asset):
            self.log.debug("%s not covered by any on-chain source; skipping", self.base_asset)
            return None
        now = int(self._now())
        return self.source.read(
            self.base_asset,
            now - self.window_seconds,
            now,
            price_usd=self._latest_price_usd(),
            whale_usd=self.whale_usd,
            deadband=self.flow_deadband,
        )

    # ── normalize (stamp the reading into a row) ───────────────────
    def normalize(self, reading: dict | None) -> list[Mapping[str, Any]]:
        """A source reading -> one ``onchain_data`` row (or nothing to write)."""
        if reading is None:
            return []
        row = dict(reading)
        row["ts"] = int(self._now())
        row["symbol"] = self.symbol
        return [row]

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

ETHERSCAN_BASE_URL = "https://api.etherscan.io/v2/api"  # unified multichain v2

# Built-in starter registry, used when config provides no ``onchain.etherscan.chains``.
# One entry per EVM chain; Etherscan's v2 API serves them all from one key by
# ``chainid``. ``cex_addresses`` are that chain's known CEX hot wallets and
# ``tokens`` maps a base asset to its contract on that chain. Edit these in
# config.yaml (``onchain.etherscan.chains``) — no code change needed. Native coins
# (BTC, native ETH, SOL, …) live on non-EVM ledgers and are intentionally absent
# (DefiLlama covers several of those via chain TVL — see :class:`DefiLlamaSource`).
DEFAULT_CHAINS: dict[str, dict[str, Any]] = {
    "ethereum": {
        "chain_id": 1,
        "cex_addresses": [
            "0x28c6c06298d514db089934071355e5743bf21d60",  # Binance 14
            "0x21a31ee1afc51d94c2efccaa2092ad1028285549",  # Binance 15
            "0xdfd5293d8e347dfe59e90efd55b2956a1343963d",  # Binance 16
            "0x56eddb7aa87536c09ccc2793473599fd21a8b17f",  # Binance 17
        ],
        "tokens": {
            "LINK": "0x514910771af9ca656af840dff83e8264ecf986ca",
            "UNI": "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984",
            "AAVE": "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9",
        },
    },
}


@dataclass(frozen=True)
class ChainConfig:
    """One EVM chain's flow registry: its id, CEX hot wallets, and token contracts.

    Addresses are lowercased and token keys upper-cased on construction so lookups
    are case-insensitive regardless of how they were written in config.
    """

    name: str
    chain_id: int
    cex_addresses: tuple[str, ...]
    tokens: Mapping[str, str]  # UPPER base asset -> lowercased contract address

    @classmethod
    def from_dict(cls, name: str, d: Mapping[str, Any]) -> "ChainConfig":
        return cls(
            name=name,
            chain_id=int(d.get("chain_id", 1)),
            cex_addresses=tuple(str(a).lower() for a in (d.get("cex_addresses") or [])),
            tokens={str(k).upper(): str(v).lower() for k, v in (d.get("tokens") or {}).items()},
        )


class EtherscanSource(TransferSource):
    """Real CEX flows from Etherscan's free **multichain** tier (ERC-20 only).

    For a covered token it queries each known CEX hot wallet's token transfers
    (``action=tokentx``) on the chain that token lives on, classifying each as
    inflow (``to`` a CEX) or outflow (``from`` a CEX). An asset present on several
    configured chains is summed across them. Uncovered assets return ``None`` so
    the collector degrades gracefully (FR-DC-4).

    Coverage is config-driven via :class:`ChainConfig` (built from
    ``onchain.etherscan.chains`` in config.yaml, or :data:`DEFAULT_CHAINS`
    otherwise), so the operator edits addresses/tokens without touching code. The
    base :class:`TransferSource` reduces the transfers to USD flows; ``urllib`` is
    the only non-pure part — parsing stays in the pure :func:`parse_tokentx`.
    """

    def __init__(
        self,
        api_key: str | None = None,
        chains: Sequence[ChainConfig] | None = None,
        base_url: str = ETHERSCAN_BASE_URL,
        timeout: float = 15.0,
        max_transfers: int = 1000,
    ) -> None:
        # Key is optional at construction; absence is reported per-asset as
        # "source unavailable" rather than crashing the collector.
        self.api_key = api_key or os.environ.get("ETHERSCAN_API_KEY", "")
        self.chains = list(chains) if chains is not None else [
            ChainConfig.from_dict(name, d) for name, d in DEFAULT_CHAINS.items()
        ]
        self.base_url = base_url
        self.timeout = timeout
        self.max_transfers = max_transfers
        self.log = logging.getLogger("collector.onchain.etherscan")

    @classmethod
    def from_config(
        cls, etherscan_cfg: Mapping[str, Any] | None, api_key: str | None = None, **kwargs: Any
    ) -> "EtherscanSource":
        """Build from the ``onchain.etherscan`` block (uses ``DEFAULT_CHAINS`` if none)."""
        chains_cfg = (etherscan_cfg or {}).get("chains")
        chains = (
            [ChainConfig.from_dict(name, d or {}) for name, d in chains_cfg.items()]
            if chains_cfg
            else None
        )
        return cls(api_key=api_key, chains=chains, **kwargs)

    def covers(self, base_asset: str) -> bool:
        key = base_asset.upper()
        return any(key in c.tokens for c in self.chains)

    def transfers(
        self, base_asset: str, since_ts: int, now_ts: int
    ) -> list[Transfer] | None:
        key = base_asset.upper()
        # Every (chain, contract) this asset is configured on (usually exactly one).
        targets = [(c, c.tokens[key]) for c in self.chains if key in c.tokens]
        if not targets:
            return None  # not a token we cover on any chain → uncovered asset
        if not self.api_key:
            self.log.warning("ETHERSCAN_API_KEY not set; on-chain source unavailable")
            return None

        out: list[Transfer] = []
        for chain, contract in targets:
            for addr in chain.cex_addresses:
                try:
                    payload = self._get_tokentx(chain.chain_id, addr, contract)
                except Exception:  # noqa: BLE001 — one address must not sink the rest
                    self.log.exception(
                        "etherscan tokentx failed (chain=%s addr=%s); skipping", chain.name, addr
                    )
                    continue
                out.extend(parse_tokentx(payload, addr, since_ts))
        return out

    def _get_tokentx(self, chain_id: int, address: str, contract: str) -> Mapping[str, Any]:
        """One Etherscan ``tokentx`` request for a (chain, address, contract)."""
        params = {
            "chainid": chain_id,
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


# ── alternative source: DefiLlama free TVL momentum (no key, multi-chain) ─────

DEFILLAMA_BASE_URL = "https://api.llama.fi"
DEFAULT_DEFILLAMA_LOOKBACK_DAYS = 1  # compare TVL now vs this many days ago

# base asset -> DefiLlama chain slug. Covers L1/L2 ecosystems Etherscan cannot
# (native coins on non-EVM chains). Edit in config.yaml (``onchain.defillama.chains``).
DEFAULT_DEFILLAMA_CHAINS: dict[str, str] = {
    "ETH": "Ethereum",
    "SOL": "Solana",
    "AVAX": "Avalanche",
    "NEAR": "Near",
    "SUI": "Sui",
    "INJ": "Injective",
    "BTC": "Bitcoin",
}


class DefiLlamaSource(OnChainSource):
    """On-chain reading from **DefiLlama chain TVL momentum** (free, no key).

    DefiLlama does not expose per-asset CEX flows, so this source measures a
    *different but real* on-chain signal: the change in **total value locked** on
    the chain a token anchors. Rising ecosystem TVL = capital flowing in
    (accumulation lean); falling = capital leaving (distribution lean). This
    naturally covers native L1/L2 coins (SOL, AVAX, ETH, …) that the ERC-20-only
    :class:`EtherscanSource` cannot.

    The reading fills ``net_flow`` (ΔTVL in USD) and ``flow_signal``; the
    CEX-specific columns (``exchange_inflow``/``outflow``/``whale_tx_count``) are
    left ``None`` — honestly "not measured by this source", not faked. ``urllib``
    is the only non-pure part; the reduction lives in the pure :func:`tvl_reading`.
    """

    def __init__(
        self,
        chains: Mapping[str, str] | None = None,
        lookback_seconds: int = DEFAULT_DEFILLAMA_LOOKBACK_DAYS * 86400,
        base_url: str = DEFILLAMA_BASE_URL,
        timeout: float = 15.0,
    ) -> None:
        src = chains if chains is not None else DEFAULT_DEFILLAMA_CHAINS
        self.chains = {k.upper(): str(v) for k, v in src.items()}
        self.lookback_seconds = lookback_seconds
        self.base_url = base_url
        self.timeout = timeout
        self.log = logging.getLogger("collector.onchain.defillama")

    @classmethod
    def from_config(
        cls, defillama_cfg: Mapping[str, Any] | None, **kwargs: Any
    ) -> "DefiLlamaSource":
        """Build from the ``onchain.defillama`` block (uses defaults if absent)."""
        cfg = defillama_cfg or {}
        chains = cfg.get("chains")  # None -> DEFAULT_DEFILLAMA_CHAINS
        if "lookback_days" in cfg:
            kwargs["lookback_seconds"] = int(float(cfg["lookback_days"]) * 86400)
        return cls(chains=chains, **kwargs)

    def covers(self, base_asset: str) -> bool:
        return base_asset.upper() in self.chains

    def read(
        self,
        base_asset: str,
        since_ts: int,
        now_ts: int,
        *,
        price_usd: float | None = None,  # unused: TVL is already USD-denominated
        whale_usd: float = 0.0,          # unused: no transfer-level data
        deadband: float = DEFAULT_FLOW_DEADBAND,
    ) -> dict | None:
        slug = self.chains.get(base_asset.upper())
        if slug is None:
            return None
        try:
            points = self._get_chain_tvl(slug)
        except Exception:  # noqa: BLE001 — a transient outage skips this cycle, never crashes
            self.log.exception("defillama chain TVL failed for %s; skipping", slug)
            return None
        return tvl_reading(points, now_ts, self.lookback_seconds, deadband)

    def _get_chain_tvl(self, chain_slug: str) -> list[Mapping[str, Any]]:
        """Daily [{date, tvl}, …] history for a chain (DefiLlama, no key)."""
        url = f"{self.base_url}/v2/historicalChainTvl/{urllib.parse.quote(chain_slug)}"
        with urllib.request.urlopen(url, timeout=self.timeout) as resp:  # noqa: S310 - fixed https host
            return json.loads(resp.read().decode())


def tvl_reading(
    points: Sequence[Mapping[str, Any]], now_ts: int, lookback_seconds: int, deadband: float
) -> dict | None:
    """Reduce a chain's TVL history to a net-flow + directional reading (pure).

    ``net_flow`` = TVL at ``now_ts`` minus TVL ``lookback_seconds`` earlier (USD).
    Positive (TVL grew) reads as accumulation, negative as distribution, with a
    deadband (fraction of current TVL) so flat TVL is neutral. Malformed points are
    skipped; too little data returns ``None``.
    """
    parsed: list[tuple[int, float]] = []
    for p in points:
        try:
            parsed.append((int(p["date"]), float(p["tvl"])))
        except (KeyError, ValueError, TypeError):
            continue  # skip malformed point rather than load garbage
    if not parsed:
        return None
    parsed.sort()

    at_or_before_now = [v for d, v in parsed if d <= now_ts]
    tvl_now = at_or_before_now[-1] if at_or_before_now else parsed[-1][1]
    at_or_before_then = [v for d, v in parsed if d <= now_ts - lookback_seconds]
    tvl_then = at_or_before_then[-1] if at_or_before_then else parsed[0][1]

    net_flow = tvl_now - tvl_then
    if tvl_now > 0 and abs(net_flow) >= deadband * tvl_now:
        signal = ACCUMULATION if net_flow > 0 else DISTRIBUTION
    else:
        signal = NEUTRAL
    return {
        "exchange_inflow": None,   # not measured by a TVL source (honest NULL)
        "exchange_outflow": None,
        "net_flow": net_flow,
        "whale_tx_count": None,
        "flow_signal": signal,
    }


# ── true-flow source: Bitcoin via Esplora (free, no key, native BTC) ─────

ESPLORA_BASE_URL = "https://blockstream.info/api"  # mempool.space mirrors the same API
SATS_PER_BTC = 100_000_000

# Built-in starter registry of known Bitcoin CEX wallets, used when config gives no
# ``onchain.bitcoin_esplora.cex_addresses``. Verified live on-chain; EDIT in
# config.yaml. Cold wallets give a slow signal (rare, large moves); a busy *hot*
# wallet (high tx_count) is where most deposit/withdrawal flow shows up — add your
# exchange's hot wallets for a livelier reading. Unlike EVM hex addresses, Bitcoin
# addresses are CASE-SENSITIVE (base58 & bech32) — store them verbatim, never lowercased.
DEFAULT_BITCOIN_CEX_ADDRESSES: tuple[str, ...] = (
    "34xp4vRoCGJym3xR7yCVPFHoCNxv4Twseo",                                # Binance cold
    "bc1qgdjqv0av3q56jvd82tkdjpy7gdp9ut8tlqmgrpmv24sq90ecnvqqjwvw97",    # Bitfinex cold
    "bc1qm34lsc65zpw79lxes69zkqmk6ee3ewf0j77s3h",                        # busy hot wallet (flow-rich)
)
DEFAULT_BITCOIN_ASSETS: tuple[str, ...] = ("BTC",)


class BitcoinEsploraSource(TransferSource):
    """Real **native-BTC** CEX flows from the free, keyless Esplora API.

    Bitcoin is the headline asset but the ERC-20-only :class:`EtherscanSource`
    cannot touch it and :class:`DefiLlamaSource` only sees its tiny BTC-DeFi TVL.
    This source closes that gap with a *true-flow* reading of the same quality class
    as Etherscan: for each known exchange address it pulls recent transactions and
    classifies each as a net deposit (onto the exchange → distribution pressure) or
    net withdrawal (leaving → accumulation), in BTC units. The base
    :class:`TransferSource` then converts to USD via the stored BTC price and reduces
    to the directional reading — so it fills **every** ``onchain_data`` flow column,
    not just ``net_flow``.

    Coverage is config-driven (``onchain.bitcoin_esplora`` in config.yaml, or the
    built-in starter registry otherwise): the watched ``assets`` (default ``BTC``)
    and the ``cex_addresses`` to scan. With no addresses the source reports the asset
    as uncovered (``covers`` → False) so a :class:`CompositeSource` cleanly falls
    through to the next provider. ``urllib`` is the only non-pure part; the UTXO
    classification lives in the pure :func:`parse_esplora_txs`.
    """

    def __init__(
        self,
        cex_addresses: Sequence[str] | None = None,
        assets: Sequence[str] | None = None,
        base_url: str = ESPLORA_BASE_URL,
        timeout: float = 15.0,
        max_pages: int = 4,
    ) -> None:
        self.cex_addresses = tuple(
            cex_addresses if cex_addresses is not None else DEFAULT_BITCOIN_CEX_ADDRESSES
        )
        self.assets = frozenset(
            str(a).upper() for a in (assets if assets is not None else DEFAULT_BITCOIN_ASSETS)
        )
        self.base_url = base_url
        self.timeout = timeout
        # Esplora returns ~25 confirmed txs/page (newest first); page back until the
        # window is covered. Bounded so a busy hot wallet can't run the cycle forever.
        self.max_pages = max_pages
        self.log = logging.getLogger("collector.onchain.bitcoin")

    @classmethod
    def from_config(
        cls, btc_cfg: Mapping[str, Any] | None, **kwargs: Any
    ) -> "BitcoinEsploraSource":
        """Build from the ``onchain.bitcoin_esplora`` block (uses defaults if absent)."""
        cfg = btc_cfg or {}
        if "max_pages" in cfg:
            kwargs["max_pages"] = int(cfg["max_pages"])
        return cls(
            cex_addresses=cfg.get("cex_addresses"),  # None -> DEFAULT_BITCOIN_CEX_ADDRESSES
            assets=cfg.get("assets"),
            **kwargs,
        )

    def covers(self, base_asset: str) -> bool:
        # No addresses ⇒ report uncovered so CompositeSource falls through (e.g. to
        # DefiLlama's BTC TVL) rather than serving an empty reading.
        return base_asset.upper() in self.assets and bool(self.cex_addresses)

    def transfers(
        self, base_asset: str, since_ts: int, now_ts: int
    ) -> list[Transfer] | None:
        if base_asset.upper() not in self.assets:
            return None  # not an asset we cover → uncovered
        if not self.cex_addresses:
            self.log.warning("no bitcoin CEX addresses configured; source unavailable")
            return None

        out: list[Transfer] = []
        for addr in self.cex_addresses:
            try:
                txs = self._get_address_txs(addr, since_ts)
            except Exception:  # noqa: BLE001 — one address must not sink the rest
                self.log.exception("esplora txs failed (addr=%s); skipping", addr)
                continue
            out.extend(parse_esplora_txs(txs, addr, since_ts))
        return out

    def _get_address_txs(self, address: str, since_ts: int) -> list[Mapping[str, Any]]:
        """Recent confirmed txs for ``address``, paged back until the window is covered.

        Esplora returns newest-first, ~25 confirmed per page; we follow the
        ``/txs/chain/{last_txid}`` cursor and stop as soon as a page's oldest tx
        predates ``since_ts`` (or ``max_pages`` is hit, so a hot wallet stays bounded).
        """
        collected: list[Mapping[str, Any]] = []
        last_txid: str | None = None
        for _ in range(self.max_pages):
            page = self._fetch_txs_page(address, last_txid)
            if not page:
                break
            collected.extend(page)
            oldest = page[-1]
            oldest_ts = (oldest.get("status") or {}).get("block_time")
            if oldest_ts is not None and int(oldest_ts) < since_ts:
                break  # we've paged past the window — older txs are irrelevant
            last_txid = oldest.get("txid")
            if not last_txid:
                break
        return collected

    def _fetch_txs_page(
        self, address: str, last_txid: str | None
    ) -> list[Mapping[str, Any]]:
        """One Esplora address-txs page (the confirmed cursor when ``last_txid`` given)."""
        path = f"/address/{urllib.parse.quote(address)}/txs"
        if last_txid:
            path += f"/chain/{urllib.parse.quote(last_txid)}"
        url = f"{self.base_url}{path}"
        with urllib.request.urlopen(url, timeout=self.timeout) as resp:  # noqa: S310 - fixed https host
            return json.loads(resp.read().decode())


def parse_esplora_txs(
    txs: Sequence[Mapping[str, Any]], address: str, since_ts: int
) -> list[Transfer]:
    """Parse Esplora address txs into net in/out CEX transfers for ``address`` (pure).

    Bitcoin is UTXO-based, so a single tx can both spend the exchange's coins (inputs)
    and pay change back to it (outputs). We therefore reduce **per transaction** to the
    address's *net* movement — outputs received minus inputs spent — and emit one
    :class:`Transfer`: net positive = a deposit ONTO the exchange (``"in"``), net
    negative = a withdrawal LEAVING it (``"out"``). This nets out change correctly and,
    across the aggregated address set, cancels internal exchange-to-exchange shuffles.

    Only **confirmed** txs at or after ``since_ts`` count (FR-DC-6: an unconfirmed,
    still-forming tx is not completed data). Malformed rows are skipped — free data is
    messy (PLAN §7, validate on intake). Amounts are satoshis converted to BTC units;
    USD conversion happens later in :func:`summarize_flows`.
    """
    transfers: list[Transfer] = []
    for tx in txs:
        status = tx.get("status") or {}
        if not status.get("confirmed"):
            continue  # still-forming / mempool tx → not completed data
        try:
            ts = int(status["block_time"])
        except (KeyError, ValueError, TypeError):
            continue
        if ts < since_ts:
            continue

        received_sats = 0  # value arriving AT the address (outputs to it)
        sent_sats = 0      # value leaving FROM the address (inputs it spent)
        for vout in tx.get("vout", []):
            try:
                if vout.get("scriptpubkey_address") == address:
                    received_sats += int(vout["value"])
            except (KeyError, ValueError, TypeError):
                continue  # skip malformed output rather than load garbage
        for vin in tx.get("vin", []):
            prevout = vin.get("prevout") or {}
            try:
                if prevout.get("scriptpubkey_address") == address:
                    sent_sats += int(prevout["value"])
            except (KeyError, ValueError, TypeError):
                continue  # skip malformed input (incl. coinbase, which has no prevout)

        net_sats = received_sats - sent_sats
        if net_sats > 0:
            transfers.append(Transfer(ts=ts, amount=net_sats / SATS_PER_BTC, direction="in"))
        elif net_sats < 0:
            transfers.append(Transfer(ts=ts, amount=-net_sats / SATS_PER_BTC, direction="out"))
        # net_sats == 0 (pure internal move / fee-only) contributes no flow
    return transfers


# ── composing sources ───────────────────────────────────────────────

class CompositeSource(OnChainSource):
    """Try several sources in order; the first that **covers** an asset serves it.

    Lets complementary providers coexist — e.g. Etherscan for ERC-20 governance
    tokens, DefiLlama for native L1 coins — so each asset is read by whichever
    source actually has data for it.
    """

    def __init__(self, sources: Sequence[OnChainSource]) -> None:
        self.sources = list(sources)

    def covers(self, base_asset: str) -> bool:
        return any(s.covers(base_asset) for s in self.sources)

    def read(
        self,
        base_asset: str,
        since_ts: int,
        now_ts: int,
        *,
        price_usd: float | None,
        whale_usd: float,
        deadband: float,
    ) -> dict | None:
        for source in self.sources:
            if source.covers(base_asset):
                return source.read(
                    base_asset, since_ts, now_ts,
                    price_usd=price_usd, whale_usd=whale_usd, deadband=deadband,
                )
        return None


# provider name -> builder from its config sub-block (api_key injected where needed).
_PROVIDER_BUILDERS = {
    "etherscan": lambda cfg, api_key: EtherscanSource.from_config(cfg.get("etherscan", {}), api_key=api_key),
    "defillama": lambda cfg, api_key: DefiLlamaSource.from_config(cfg.get("defillama", {})),
    "bitcoin_esplora": lambda cfg, api_key: BitcoinEsploraSource.from_config(cfg.get("bitcoin_esplora", {})),
}


def build_source(onchain_cfg: Mapping[str, Any] | None, api_key: str | None = None) -> OnChainSource | None:
    """Assemble the on-chain source from the config ``onchain`` block.

    ``onchain.providers`` lists the sources to use, in priority order (default:
    ``["etherscan"]``). Returns a single source, a :class:`CompositeSource` when
    several are configured, or ``None`` if none resolve.
    """
    cfg = onchain_cfg or {}
    names = cfg.get("providers") or ["etherscan"]
    log = logging.getLogger("collector.onchain")
    built: list[OnChainSource] = []
    for name in names:
        builder = _PROVIDER_BUILDERS.get(str(name).lower())
        if builder is None:
            log.warning("unknown on-chain provider %r; skipping", name)
            continue
        built.append(builder(cfg, api_key))
    if not built:
        return None
    return built[0] if len(built) == 1 else CompositeSource(built)
