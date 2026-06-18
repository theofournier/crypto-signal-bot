"""Seed historical market data from Binance Data Vision (BUILD_PLAN Phase 2).

Goal: load months of real, *closed* candles into the ``market_data`` table so the
scoring engine and backtester (later phases) can be built and validated offline
(PLAN.md §7). Binance Data Vision (``data.binance.vision``) publishes official
monthly OHLCV dumps with no API key and no rate limit — the best free seed.

Pipeline per pair/timeframe:
    download monthly ZIPs -> parse CSVs -> validate on ingest -> compute derived
    features -> load new rows into market_data (idempotent re-runs).

Design choices (matching Phase 1's ``init_db.py``):
  * **Standard library only** — no pandas/pandas-ta needed, so the seed runs with
    nothing installed. Indicators are computed with plain Python.
  * **Only completed candles** (FR-DC-6). Monthly dumps contain only closed
    candles, and we never fetch the current (still-forming) month.
  * **Validate on ingest** (PLAN §7): duplicate timestamps, missing intervals and
    structurally-invalid candles are detected and logged — never silently loaded.
  * **Loader writes only to the DB** — no decision logic lives here.

Two Binance quirks this module handles explicitly:
  1. Dumps have **no header row** (older) — but some recent ones do; both work.
  2. The timestamp **unit changed**: 2024 dumps are milliseconds, 2025+ dumps are
     microseconds. We normalize every timestamp to unix **seconds** by magnitude.
"""

from __future__ import annotations

import io
import logging
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from data import db

log = logging.getLogger("seed")

# Where downloaded ZIPs/CSVs are cached. Gitignored (.gitignore: /data/raw/, *.csv).
RAW_DIR = Path(__file__).resolve().parent / "raw"
BASE_URL = "https://data.binance.vision/data/spot/monthly/klines"

# Bollinger / RSI / volume windows. Standard periods; expressed in candles.
RSI_PERIOD = 14
BB_PERIOD = 20
VOLUME_AVG_DAYS = 30  # "volume / 30-day avg" (PLAN §5.1 / schema)


# ── timeframe helpers ───────────────────────────────────────────
_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def timeframe_seconds(timeframe: str) -> int:
    """'1h' -> 3600, '15m' -> 900, '1d' -> 86400."""
    unit = timeframe[-1].lower()
    if unit not in _UNIT_SECONDS:
        raise ValueError(f"unsupported timeframe {timeframe!r}")
    return int(timeframe[:-1]) * _UNIT_SECONDS[unit]


def _to_seconds(raw: int) -> int:
    """Normalize a Binance timestamp (s / ms / us) to unix seconds by magnitude."""
    if raw >= 1_000_000_000_000_000:   # microseconds (2025+ dumps)
        return raw // 1_000_000
    if raw >= 1_000_000_000_000:       # milliseconds (older dumps)
        return raw // 1_000
    return raw                         # already seconds


def _close_to_seconds(raw_close: int) -> int:
    """Candle close time -> clean unix seconds.

    Binance close_time is the last millisecond/microsecond of the period (e.g.
    ...799999). Adding 1 before scaling lands on the exact period boundary.
    """
    if raw_close >= 1_000_000_000_000_000:
        return (raw_close + 1) // 1_000_000
    if raw_close >= 1_000_000_000_000:
        return (raw_close + 1) // 1_000
    return raw_close


def file_symbol(symbol: str) -> str:
    """'BTC/USDT' -> 'BTCUSDT' for Binance Data Vision file paths."""
    return symbol.replace("/", "").upper()


# ── data structures ─────────────────────────────────────────────
@dataclass
class Candle:
    open_ts: int   # period open, unix seconds (the time grid)
    close_ts: int  # period close, unix seconds (stored as market_data.ts)
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class ValidationReport:
    rows_parsed: int = 0
    duplicates_dropped: int = 0
    invalid_dropped: int = 0
    missing_intervals: int = 0
    gap_examples: list[str] = field(default_factory=list)

    def log(self) -> None:
        log.info(
            "validation: parsed=%d, duplicates_dropped=%d, invalid_dropped=%d, "
            "missing_intervals=%d",
            self.rows_parsed,
            self.duplicates_dropped,
            self.invalid_dropped,
            self.missing_intervals,
        )
        for example in self.gap_examples[:10]:
            log.warning("gap: %s", example)


# ── download ────────────────────────────────────────────────────
def completed_months(n: int, today: datetime | None = None) -> list[tuple[int, int]]:
    """The last ``n`` *completed* calendar months as (year, month), oldest first.

    Never includes the current month — its monthly dump does not exist yet and its
    candles are partly still forming (FR-DC-6: completed periods only).
    """
    today = today or datetime.now(timezone.utc)
    year, month = today.year, today.month
    out: list[tuple[int, int]] = []
    for _ in range(n):
        month -= 1
        if month == 0:
            month, year = 12, year - 1
        out.append((year, month))
    return list(reversed(out))


def download_month(
    symbol: str, timeframe: str, year: int, month: int, raw_dir: Path = RAW_DIR
) -> bytes | None:
    """Fetch one monthly kline ZIP and return the inner CSV bytes (cached on disk).

    Returns ``None`` if Binance has no dump for that month (HTTP 404) — callers
    treat that as "skip", not an error.
    """
    fsym = file_symbol(symbol)
    name = f"{fsym}-{timeframe}-{year:04d}-{month:02d}"
    raw_dir.mkdir(parents=True, exist_ok=True)
    csv_path = raw_dir / f"{name}.csv"
    if csv_path.exists():
        log.info("cache hit %s", csv_path.name)
        return csv_path.read_bytes()

    url = f"{BASE_URL}/{fsym}/{timeframe}/{name}.zip"
    try:
        log.info("downloading %s", url)
        data = urllib.request.urlopen(url, timeout=60).read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            log.warning("no dump for %s (404) — skipping", name)
            return None
        raise
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        csv_bytes = zf.read(zf.namelist()[0])
    csv_path.write_bytes(csv_bytes)  # cache the extracted CSV for re-runs
    return csv_bytes


# ── parse ───────────────────────────────────────────────────────
def parse_csv(csv_bytes: bytes) -> list[Candle]:
    """Parse Binance kline CSV bytes into Candles. Tolerates an optional header.

    Kline columns: open_time, open, high, low, close, volume, close_time, ...
    """
    candles: list[Candle] = []
    for line in csv_bytes.decode().splitlines():
        if not line.strip():
            continue
        fields = line.split(",")
        try:
            open_raw = int(fields[0])
        except ValueError:
            continue  # header row ("open_time,...") — skip it
        candles.append(
            Candle(
                open_ts=_to_seconds(open_raw),
                close_ts=_close_to_seconds(int(fields[6])),
                open=float(fields[1]),
                high=float(fields[2]),
                low=float(fields[3]),
                close=float(fields[4]),
                volume=float(fields[5]),
            )
        )
    return candles


# ── validate on ingest (PLAN §7) ────────────────────────────────
def _is_valid(c: Candle) -> bool:
    """Structural sanity: positive prices, high is the max, low is the min."""
    if min(c.open, c.high, c.low, c.close) <= 0 or c.volume < 0:
        return False
    if c.high < max(c.open, c.close) or c.low > min(c.open, c.close):
        return False
    return c.high >= c.low


def validate(candles: list[Candle], timeframe: str) -> tuple[list[Candle], ValidationReport]:
    """Sort, drop duplicate/invalid candles, and report missing intervals.

    Duplicates and structurally-broken candles are removed (and counted); gaps in
    the time grid are reported but cannot be filled — they are logged so a later
    backtest is never silently run over holes.
    """
    report = ValidationReport(rows_parsed=len(candles))
    candles = sorted(candles, key=lambda c: c.open_ts)

    deduped: list[Candle] = []
    seen: set[int] = set()
    for c in candles:
        if c.open_ts in seen:
            report.duplicates_dropped += 1
            continue
        if not _is_valid(c):
            report.invalid_dropped += 1
            log.warning("invalid candle @ %s dropped", _iso(c.open_ts))
            continue
        seen.add(c.open_ts)
        deduped.append(c)

    step = timeframe_seconds(timeframe)
    for prev, cur in zip(deduped, deduped[1:]):
        gap = cur.open_ts - prev.open_ts
        if gap != step:
            missing = gap // step - 1
            report.missing_intervals += max(missing, 0)
            report.gap_examples.append(
                f"{missing} interval(s) missing between {_iso(prev.open_ts)} "
                f"and {_iso(cur.open_ts)}"
            )
    return deduped, report


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")


# ── derived features (pure-Python indicators) ───────────────────
def _rsi(closes: list[float]) -> list[float | None]:
    """Wilder's RSI(14). None until enough history exists."""
    n = RSI_PERIOD
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n
    out[n] = _rsi_value(avg_gain, avg_loss)
    for i in range(n + 1, len(closes)):
        avg_gain = (avg_gain * (n - 1) + gains[i - 1]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i - 1]) / n
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _bb_width(closes: list[float]) -> list[float | None]:
    """Bollinger Band width = (upper - lower) / middle = 4*std / SMA, period 20."""
    n = BB_PERIOD
    out: list[float | None] = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        window = closes[i - n + 1 : i + 1]
        mean = sum(window) / n
        var = sum((x - mean) ** 2 for x in window) / n
        std = var ** 0.5
        out[i] = (4.0 * std / mean) if mean else None
    return out


def _volume_ratio(volumes: list[float], window: int) -> list[float | None]:
    """volume / trailing average volume over ``window`` candles (~30 days)."""
    out: list[float | None] = [None] * len(volumes)
    for i in range(window - 1, len(volumes)):
        avg = sum(volumes[i - window + 1 : i + 1]) / window
        out[i] = (volumes[i] / avg) if avg else None
    return out


def _vwap_distance(candles: list[Candle]) -> list[float | None]:
    """Percent distance of close from a UTC-day-anchored VWAP.

    VWAP resets each calendar day (the standard intraday anchor). Distance is
    (close - vwap) / vwap * 100.
    """
    out: list[float | None] = []
    cur_day: str | None = None
    cum_pv = cum_v = 0.0
    for c in candles:
        day = _iso(c.open_ts)[:10]
        if day != cur_day:
            cur_day, cum_pv, cum_v = day, 0.0, 0.0
        typical = (c.high + c.low + c.close) / 3.0
        cum_pv += typical * c.volume
        cum_v += c.volume
        if cum_v:
            vwap = cum_pv / cum_v
            out.append((c.close - vwap) / vwap * 100.0)
        else:
            out.append(None)
    return out


def build_rows(candles: list[Candle], symbol: str, timeframe: str) -> list[dict[str, Any]]:
    """Turn validated candles into market_data rows with derived features.

    ``bid_ask_imbalance`` stays NULL: order-book pressure is not present in OHLCV
    dumps (BUILD_PLAN Phase 2: compute features "where available"). It is filled
    by the live collector in Phase 3.
    """
    closes = [c.close for c in candles]
    volumes = [c.volume for c in candles]
    window = max(VOLUME_AVG_DAYS * 86400 // timeframe_seconds(timeframe), 1)

    rsi = _rsi(closes)
    bbw = _bb_width(closes)
    vol_ratio = _volume_ratio(volumes, window)
    vwap_dist = _vwap_distance(candles)

    rows: list[dict[str, Any]] = []
    for i, c in enumerate(candles):
        rows.append(
            {
                "ts": c.close_ts,
                "symbol": symbol,
                "timeframe": timeframe,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
                "volume_ratio": vol_ratio[i],
                "bid_ask_imbalance": None,  # not in OHLCV dumps; live-only feature
                "bb_width": bbw[i],
                "rsi": rsi[i],
                "vwap_distance": vwap_dist[i],
            }
        )
    return rows


# ── orchestration ───────────────────────────────────────────────
def seed(
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    months: int = 6,
    conn=None,
    raw_dir: Path = RAW_DIR,
) -> int:
    """Download, validate, feature-build and load ``months`` of history for a pair.

    Idempotent: candle timestamps already present for this (symbol, timeframe) are
    skipped, so re-running never duplicates rows. Returns the number of new rows
    inserted into market_data.
    """
    owns_conn = conn is None
    conn = conn or db.connect()
    try:
        candles: list[Candle] = []
        for year, month in completed_months(months):
            csv_bytes = download_month(symbol, timeframe, year, month, raw_dir)
            if csv_bytes is not None:
                candles.extend(parse_csv(csv_bytes))

        if not candles:
            log.error("no candles downloaded for %s %s — nothing to load", symbol, timeframe)
            return 0

        candles, report = validate(candles, timeframe)
        report.log()

        rows = build_rows(candles, symbol, timeframe)
        rows = _drop_existing(conn, symbol, timeframe, rows)
        inserted = db.insert_many(conn, "market_data", rows)
        log.info(
            "loaded %d new rows for %s %s (%d already present, skipped)",
            inserted, symbol, timeframe, len(candles) - inserted,
        )
        return inserted
    finally:
        if owns_conn:
            conn.close()


def _drop_existing(conn, symbol: str, timeframe: str, rows: list[dict]) -> list[dict]:
    """Filter out rows whose ts is already stored (keeps re-runs idempotent)."""
    existing = {
        r["ts"]
        for r in db.query(
            conn,
            "SELECT ts FROM market_data WHERE symbol = ? AND timeframe = ?",
            (symbol, timeframe),
        )
    }
    return [r for r in rows if r["ts"] not in existing]
