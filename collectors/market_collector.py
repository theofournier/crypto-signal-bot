"""Live market collector (BUILD_PLAN Phase 3).

Polls an exchange for recent OHLCV via CCXT and appends new rows to
``market_data`` — the live counterpart of the Phase 2 seed. By design it reuses
the seed's parse/validate/feature pipeline verbatim, so a live row and a seeded
row for the same candle are **byte-for-byte identical** (BUILD_PLAN Phase 3:
"compute the same derived features as the seed").

Two rules dominate this file:

  * **Closed candles only — never repaint** (FR-DC-6, PLAN §2.3). The exchange's
    most recent candle is still forming; acting on it would produce fake history.
    ``_closed_candles`` drops any candle whose period has not fully elapsed, so a
    still-forming candle is never written. This is the repainting guard.
  * **Observe, never decide** (FR-DC-5). This collector only writes rows. There
    is zero scoring/trading logic here.

**Transport choice — REST polling, not a WebSocket.** PLAN §5.1 sketches a
WebSocket subscription, but the base pattern it defines is a ``fetch -> sleep``
poll loop, and the closed-candle guard is required regardless of transport (even
a streamed candle arrives partial first). On the 1h timeframe, polling
``fetch_ohlcv`` every cycle is simple, robust, idempotent, and well within
NFR-PERF (we do not compete on sub-second execution). A ccxt.pro WebSocket feed
is a future optimization for sub-minute timeframes; it would not change what gets
stored. CCXT is imported lazily so this module (and its tests) load without it.

Indicator warm-up: features like ``volume_ratio`` need ~30 days of trailing
candles. Each cycle we prepend recent rows already in the DB (the seed, or prior
live rows) as context, recompute features over the combined tail with the seed's
own functions, then insert only the candles not yet stored. New rows therefore
land with fully warmed indicators, and re-runs are idempotent.
"""

from __future__ import annotations

import time
from typing import Any, Mapping

from collectors.base_collector import BaseCollector
from data import db, seed


def _default_exchange(name: str):
    """Build a CCXT exchange for public market data (no API key needed for reads).

    Imported lazily so the module and tests do not require ccxt to be installed;
    only an actual live run needs it.
    """
    try:
        import ccxt  # noqa: PLC0415 — lazy, optional dependency
    except ImportError as exc:  # pragma: no cover - exercised only without ccxt
        raise RuntimeError(
            "ccxt is required for live collection: pip install ccxt"
        ) from exc
    return getattr(ccxt, name)({"enableRateLimit": True})


class MarketCollector(BaseCollector):
    """Append closed candles + derived features for ONE pair to ``market_data``.

    One collector handles a single symbol; ``run_collectors.py`` runs one per pair
    so a failure on one pair never stalls another (FR-DC-4).
    """

    table = "market_data"

    def __init__(
        self,
        conn,
        symbol: str,
        timeframe: str = "1h",
        exchange: Any | None = None,
        exchange_name: str = "binance",
        interval: float | None = None,
        fetch_limit: int = 300,
        now_fn=time.time,
    ) -> None:
        # Poll a little faster than the candle period so a freshly closed candle
        # is picked up promptly (default: a third of the timeframe).
        tf_seconds = seed.timeframe_seconds(timeframe)
        super().__init__(conn, interval or max(tf_seconds // 3, 5), name=f"market:{symbol}")
        self.symbol = symbol
        self.timeframe = timeframe
        self.tf_seconds = tf_seconds
        self.exchange = exchange if exchange is not None else _default_exchange(exchange_name)
        # How many recent candles to pull each cycle (covers short downtime gaps).
        self.fetch_limit = fetch_limit
        # Context pulled from the DB to warm the longest indicator (~30d volume).
        volume_window = max(seed.VOLUME_AVG_DAYS * 86400 // tf_seconds, 1)
        self.context_candles = volume_window + seed.BB_PERIOD
        self._now = now_fn

    # ── fetch ──────────────────────────────────────────────────────
    def fetch(self) -> list[list[float]]:
        """Pull recent OHLCV: [[open_ms, open, high, low, close, volume], ...].

        The last entry is the still-forming candle; ``normalize`` discards it.
        """
        return self.exchange.fetch_ohlcv(self.symbol, self.timeframe, limit=self.fetch_limit)

    # ── normalize ──────────────────────────────────────────────────
    def normalize(self, raw: list[list[float]]) -> list[Mapping[str, Any]]:
        """Raw OHLCV -> new ``market_data`` rows (closed candles only, warmed).

        Pipeline (all seed functions, so features match the seed exactly):
          1. raw -> Candle objects (open_ms -> unix-second grid)
          2. drop still-forming candles (repainting guard, FR-DC-6)
          3. prepend recent DB rows as indicator-warm-up context
          4. validate (sort/dedup/gap-check) + build feature rows
          5. keep only candles not already stored (idempotent)
        """
        fetched = [self._to_candle(row) for row in raw]
        closed = self._closed_candles(fetched)
        if not closed:
            return []

        candles = self._context_from_db() + closed
        validated, report = seed.validate(candles, self.timeframe)
        if report.missing_intervals:
            report.log()

        rows = seed.build_rows(validated, self.symbol, self.timeframe)
        return seed._drop_existing(self.conn, self.symbol, self.timeframe, rows)

    # ── helpers ────────────────────────────────────────────────────
    def _to_candle(self, row: list[float]) -> seed.Candle:
        """One CCXT OHLCV row -> a seed Candle on the unix-second grid.

        CCXT timestamps are the candle's OPEN time in milliseconds; the close
        time is one period later (the clean boundary the seed also stores).
        """
        open_ts = int(row[0]) // 1000
        return seed.Candle(
            open_ts=open_ts,
            close_ts=open_ts + self.tf_seconds,
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
        )

    def _closed_candles(self, candles: list[seed.Candle]) -> list[seed.Candle]:
        """Keep only candles whose period has fully elapsed (FR-DC-6).

        A candle is closed iff its close time is at or before now. This drops the
        exchange's last, still-forming candle — the repainting guard — and is the
        single point that decides what is safe to store.
        """
        now = self._now()
        return [c for c in candles if c.close_ts <= now]

    def _context_from_db(self) -> list[seed.Candle]:
        """Recent stored candles, reconstructed for indicator warm-up.

        Returns the last ``context_candles`` rows for this pair as Candle objects
        so ``build_rows`` recomputes features with full trailing history. Empty on
        a fresh DB — features then warm up over the fetched window, exactly as the
        seed's early rows do.
        """
        rows = db.query(
            self.conn,
            "SELECT ts, open, high, low, close, volume FROM market_data "
            "WHERE symbol = ? AND timeframe = ? ORDER BY ts DESC LIMIT ?",
            (self.symbol, self.timeframe, self.context_candles),
        )
        return [
            seed.Candle(
                open_ts=r["ts"] - self.tf_seconds,
                close_ts=r["ts"],
                open=r["open"],
                high=r["high"],
                low=r["low"],
                close=r["close"],
                volume=r["volume"],
            )
            for r in reversed(rows)  # oldest first, to match the time grid
        ]
