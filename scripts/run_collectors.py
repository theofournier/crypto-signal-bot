"""Launch the live collectors (BUILD_PLAN Phase 3 / Phase 9).

Each collector runs one independent loop per configured pair (FR-DC-4 — a failure
on one pair never stalls another): the **market** collector appends closed candles
to ``market_data``, the **on-chain** collector appends exchange-flow observations
to ``onchain_data``, and (Phase 9) the **sentiment** collector appends a rolling
social/news mood to ``sentiment_data``.

Usage:
    python3 scripts/run_collectors.py                 # pairs/timeframe from config
    python3 scripts/run_collectors.py --pair BTC/USDT --timeframe 1h
    python3 scripts/run_collectors.py --once          # one cycle then exit (smoke test)
    python3 scripts/run_collectors.py --no-onchain    # skip the on-chain collectors
    python3 scripts/run_collectors.py --no-market     # skip the market collectors
    python3 scripts/run_collectors.py --no-sentiment  # skip the sentiment collectors

Reads the asset universe from config/config.yaml, falling back to the committed
config/config.example.yaml. Requires ccxt for live exchange access; the on-chain
collector needs ETHERSCAN_API_KEY and the sentiment collector's Reddit/Telegram
sources need their keys in secrets.env (each degrades to a no-op without — the
keyless Fear & Greed and RSS sentiment sources still run).
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

# Allow running as a plain script (python3 scripts/run_collectors.py).
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from collectors.market_collector import MarketCollector  # noqa: E402
from collectors.onchain_collector import OnChainCollector, build_source  # noqa: E402
from collectors.sentiment_collector import (  # noqa: E402
    SentimentCollector,
    build_source as build_sentiment_source,
)
from core import scoring  # noqa: E402 — reuse the one canonical config loader
from data import db  # noqa: E402

log = logging.getLogger("run_collectors")


def load_secrets_into_env() -> None:
    """Load config/secrets.env (gitignored) into the process environment.

    So the on-chain source picks up ``ETHERSCAN_API_KEY`` without each component
    reimplementing dotenv parsing. Real environment variables win over the file,
    and a missing file is fine (the source degrades to a no-op — FR-DC-4).
    """
    path = ROOT / "config" / "secrets.env"
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value:
            os.environ.setdefault(key, value)


def _onchain_kwargs(onchain_cfg: dict) -> dict:
    """Translate the config ``onchain`` block into OnChainCollector kwargs."""
    kwargs: dict = {}
    if "poll_seconds" in onchain_cfg:
        kwargs["interval"] = float(onchain_cfg["poll_seconds"])
    if "window_hours" in onchain_cfg:
        kwargs["window_seconds"] = int(float(onchain_cfg["window_hours"]) * 3600)
    if "whale_usd" in onchain_cfg:
        kwargs["whale_usd"] = float(onchain_cfg["whale_usd"])
    if "flow_deadband" in onchain_cfg:
        kwargs["flow_deadband"] = float(onchain_cfg["flow_deadband"])
    return kwargs


def _sentiment_kwargs(sentiment_cfg: dict) -> dict:
    """Translate the config ``sentiment`` block into SentimentCollector kwargs."""
    kwargs: dict = {}
    if "poll_seconds" in sentiment_cfg:
        kwargs["interval"] = float(sentiment_cfg["poll_seconds"])
    if "window_hours" in sentiment_cfg:
        kwargs["window_seconds"] = int(float(sentiment_cfg["window_hours"]) * 3600)
    return kwargs


def main() -> int:
    parser = argparse.ArgumentParser(description="Run live collectors (market + on-chain + sentiment)")
    parser.add_argument("--pair", action="append", help="override pair(s); repeatable")
    parser.add_argument("--timeframe", help="override timeframe (e.g. 1h)")
    parser.add_argument("--exchange", help="override exchange (e.g. binance)")
    parser.add_argument("--once", action="store_true", help="run one cycle per pair then exit")
    parser.add_argument("--no-onchain", action="store_true", help="skip the on-chain collectors")
    parser.add_argument("--no-market", action="store_true", help="skip the market collectors")
    parser.add_argument("--no-sentiment", action="store_true", help="skip the sentiment collectors")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    cfg = scoring.load_config()
    universe = cfg.get("universe", {})
    pairs = args.pair or universe.get("pairs", ["BTC/USDT"])
    timeframe = args.timeframe or universe.get("timeframe", "1h")
    exchange_name = args.exchange or universe.get("exchange", "binance")

    market_enabled = not args.no_market

    secrets_loaded = False  # load secrets.env at most once, only if a source needs it

    onchain_cfg = cfg.get("onchain", {})
    onchain_enabled = bool(onchain_cfg.get("enabled", True)) and not args.no_onchain
    onchain_kwargs = _onchain_kwargs(onchain_cfg)
    onchain_source = None
    if onchain_enabled:
        load_secrets_into_env()  # so EtherscanSource finds ETHERSCAN_API_KEY
        secrets_loaded = True
        # Stateless (config + stdlib HTTP) → one instance is safely shared by all
        # pair threads. build_source assembles the providers listed in
        # onchain.providers (etherscan and/or defillama) into one source.
        onchain_source = build_source(onchain_cfg)
        if onchain_source is None:
            log.warning("on-chain enabled but no providers resolved; disabling")
            onchain_enabled = False

    sentiment_cfg = cfg.get("sentiment", {})
    sentiment_enabled = bool(sentiment_cfg.get("enabled", True)) and not args.no_sentiment
    sentiment_kwargs = _sentiment_kwargs(sentiment_cfg)
    sentiment_source = None
    if sentiment_enabled:
        if not secrets_loaded:
            load_secrets_into_env()  # so Reddit/Telegram sources find their creds
            secrets_loaded = True
        # Stateless (config + stdlib HTTP / lazy clients) → one instance shared by
        # all pair threads. build_sentiment_source blends the providers listed in
        # sentiment.providers (fear_greed/rss/reddit/telegram) into one source.
        sentiment_source = build_sentiment_source(sentiment_cfg)
        if sentiment_source is None:
            log.warning("sentiment enabled but no providers resolved; disabling")
            sentiment_enabled = False

    if not market_enabled and not onchain_enabled and not sentiment_enabled:
        log.error("nothing to run: every collector is disabled")
        return 2

    log.info("collectors: %s @ %s on %s (market: %s, on-chain: %s, sentiment: %s)",
             pairs, timeframe, exchange_name,
             "on" if market_enabled else "off",
             "on" if onchain_enabled else "off",
             "on" if sentiment_enabled else "off")

    if args.once:
        # Single-threaded smoke test: one connection in this thread is fine.
        conn = db.connect()
        try:
            if market_enabled:
                for p in pairs:
                    MarketCollector(
                        conn, symbol=p, timeframe=timeframe, exchange_name=exchange_name
                    ).run_once()
            if onchain_enabled:
                for p in pairs:
                    OnChainCollector(
                        conn, symbol=p, source=onchain_source, timeframe=timeframe, **onchain_kwargs
                    ).run_once()
            if sentiment_enabled:
                for p in pairs:
                    SentimentCollector(
                        conn, symbol=p, source=sentiment_source, timeframe=timeframe, **sentiment_kwargs
                    ).run_once()
        finally:
            conn.close()
        return 0

    _run_forever(
        pairs, timeframe, exchange_name, market_enabled,
        onchain_source, onchain_kwargs, sentiment_source, sentiment_kwargs,
    )
    return 0


def _market_worker(symbol: str, timeframe: str, exchange_name: str, stop: threading.Event) -> None:
    """Run one pair's market collector with its OWN connection + exchange.

    A SQLite connection can only be used in the thread that created it, so each
    collector opens its own here (cheap; SQLite handles multiple connections to
    one file). The ccxt exchange is likewise built per-thread for safety.
    """
    conn = db.connect()
    try:
        MarketCollector(
            conn, symbol=symbol, timeframe=timeframe, exchange_name=exchange_name
        ).run(stop)
    finally:
        conn.close()


def _onchain_worker(symbol, timeframe, source, kwargs, stop: threading.Event) -> None:
    """Run one pair's on-chain collector with its OWN connection.

    The SQLite connection is per-thread (it cannot be shared); the on-chain
    ``source`` is stateless config + stdlib HTTP, so all pairs share one instance.
    Without an API key the source degrades to writing nothing, never a crash
    (FR-DC-4).
    """
    conn = db.connect()
    try:
        OnChainCollector(
            conn, symbol=symbol, source=source, timeframe=timeframe, **kwargs
        ).run(stop)
    finally:
        conn.close()


def _sentiment_worker(symbol, timeframe, source, kwargs, stop: threading.Event) -> None:
    """Run one pair's sentiment collector with its OWN connection.

    Same shape as the on-chain worker: per-thread SQLite connection, one shared
    stateless source. Missing deps/keys degrade the source to writing nothing,
    never a crash (FR-DC-4).
    """
    conn = db.connect()
    try:
        SentimentCollector(
            conn, symbol=symbol, source=source, timeframe=timeframe, **kwargs
        ).run(stop)
    finally:
        conn.close()


def _run_forever(
    pairs: list[str],
    timeframe: str,
    exchange_name: str,
    market_enabled: bool,
    onchain_source,
    onchain_kwargs: dict,
    sentiment_source=None,
    sentiment_kwargs: dict | None = None,
) -> None:
    """One thread per (collector, pair); stop them all cleanly on Ctrl-C/SIGTERM."""
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    threads = []
    if market_enabled:
        threads += [
            threading.Thread(
                target=_market_worker, args=(p, timeframe, exchange_name, stop),
                name=f"market:{p}", daemon=True,
            )
            for p in pairs
        ]
    if onchain_source is not None:
        threads += [
            threading.Thread(
                target=_onchain_worker, args=(p, timeframe, onchain_source, onchain_kwargs, stop),
                name=f"onchain:{p}", daemon=True,
            )
            for p in pairs
        ]
    if sentiment_source is not None:
        threads += [
            threading.Thread(
                target=_sentiment_worker,
                args=(p, timeframe, sentiment_source, sentiment_kwargs or {}, stop),
                name=f"sentiment:{p}", daemon=True,
            )
            for p in pairs
        ]
    for t in threads:
        t.start()
    log.info("running %d collector thread(s); Ctrl-C to stop", len(threads))
    while not stop.wait(1.0):
        pass
    log.info("shutting down")
    for t in threads:
        t.join(timeout=5.0)


if __name__ == "__main__":
    raise SystemExit(main())
