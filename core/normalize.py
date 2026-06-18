"""Stage 1 of the engine: raw features -> 0-100 sub-score + direction (PLAN §5.3).

Each information source speaks a different language — a 3.2x volume ratio, an RSI
of 61, a +1.8% distance from VWAP cannot be averaged raw. Normalization squashes
each source onto a **common 0-100 sub-score** (50 = neutral) plus a **direction**
(``long`` / ``bearish`` / ``none``) so the composite stage can fuse them
arithmetically. This module is pure: it reads feature values and returns numbers,
touching neither the DB nor any decision/trade logic.

Phase 4 scope (BUILD_PLAN): only the **market** source is live, so only
``market_subscore`` is real. ``onchain_subscore`` and ``sentiment_subscore``
return neutral, **inactive** placeholders — they become real in Phase 9. The gate
(``core/scoring.py``) only requires agreement among *active* sources, so an
inactive placeholder neither votes nor blocks (BUILD_PLAN Phase 4 note: "with only
the market source live, agreement is trivially the market direction").

How the market sub-score is built (transparent, rule-based — no ML yet):

  * **Momentum (RSI):** RSI is already 0-100 with 50 neutral; used directly.
  * **Trend (VWAP distance):** % above VWAP is bullish, below is bearish; mapped
    onto 0-100 around the 50 midpoint by ``VWAP_SENSITIVITY``.
  * **Volume confirmation (volume_ratio):** not directional on its own — it scales
    how far the blended reading is allowed to stray from neutral. A move on heavy
    volume is trusted (amplified); the same move on thin volume is damped toward 50.

``bb_width`` (volatility) and ``bid_ask_imbalance`` (often NULL in historical
OHLCV) are deliberately NOT in the directional score: volatility is a risk-gate
input (Phase 5), not a direction. Missing features fall back to neutral so a
warming-up indicator never fabricates a direction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

# Direction vocabulary shared across the engine. v1 is long-or-flat (PLAN §1):
# "bearish" means "do not enter / exit a long", never "open a short".
LONG = "long"
BEARISH = "bearish"
NONE = "none"

NEUTRAL = 50.0  # midpoint of the 0-100 scale: no directional information

# ── market normalization tunables (documented defaults) ─────────────
# Internal blend of the two directional components (sum to 1.0). Trend is weighted
# a touch higher than raw momentum; both are hard price data.
W_RSI = 0.45
W_VWAP = 0.55
# +1% above VWAP shifts the trend component by this many points off neutral.
# 50 / 5 means a +5% extension saturates the component at 100.
VWAP_SENSITIVITY = 10.0
# Volume confirmation clamp: thin volume (<1x avg) damps the reading toward
# neutral; heavy volume (>1x) amplifies it, up to 1.5x.
VOLUME_FACTOR_MIN = 0.5
VOLUME_FACTOR_MAX = 1.5
# How far the sub-score must sit from neutral to commit to a direction. Inside the
# +/- band the reading is "none" (too close to call) — the first noise filter.
DIRECTION_BAND = 10.0


@dataclass
class SubScore:
    """One source's normalized reading.

    ``score`` is 0-100 (50 neutral). ``direction`` is long/bearish/none.
    ``active`` is False for a placeholder source that should not vote in the gate
    nor count toward agreement. ``detail`` is a short human-readable trace that
    feeds the signal's ``reason`` (FR-SG-3).
    """

    score: float
    direction: str
    active: bool
    detail: str


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _direction_for(score: float) -> str:
    """Commit to a direction only outside the neutral band (PLAN §5.3 gate)."""
    if score >= NEUTRAL + DIRECTION_BAND:
        return LONG
    if score <= NEUTRAL - DIRECTION_BAND:
        return BEARISH
    return NONE


def market_subscore(row: Mapping[str, object]) -> SubScore:
    """Turn one ``market_data`` row into a 0-100 sub-score + direction.

    Each feature that is missing (NULL during indicator warm-up) falls back to a
    neutral contribution, so an early candle is reported as ``none`` rather than
    given a fabricated direction.
    """
    rsi = _as_float(_get(row, "rsi"))
    vwap_distance = _as_float(_get(row, "vwap_distance"))
    volume_ratio = _as_float(_get(row, "volume_ratio"))

    # Momentum: RSI is already on a 0-100 / 50-neutral scale.
    rsi_score = _clamp(rsi, 0.0, 100.0) if rsi is not None else NEUTRAL
    # Trend: distance from VWAP, mapped around the neutral midpoint.
    if vwap_distance is not None:
        vwap_score = _clamp(NEUTRAL + vwap_distance * VWAP_SENSITIVITY, 0.0, 100.0)
    else:
        vwap_score = NEUTRAL

    base = W_RSI * rsi_score + W_VWAP * vwap_score

    # Volume confirmation scales the deviation from neutral (not the direction).
    if volume_ratio is not None:
        volume_factor = _clamp(volume_ratio, VOLUME_FACTOR_MIN, VOLUME_FACTOR_MAX)
    else:
        volume_factor = 1.0
    score = _clamp(NEUTRAL + (base - NEUTRAL) * volume_factor, 0.0, 100.0)

    direction = _direction_for(score)
    detail = (
        f"market={score:.1f} ({direction}): "
        f"rsi={_fmt(rsi)}, vwap_dist={_fmt(vwap_distance)}%, "
        f"vol_ratio={_fmt(volume_ratio)}"
    )
    return SubScore(score=score, direction=direction, active=True, detail=detail)


def onchain_subscore(row: Mapping[str, object] | None = None) -> SubScore:
    """Neutral placeholder until Phase 9 wires the on-chain collector (BUILD_PLAN).

    Inactive: it neither votes in the gate nor counts toward agreement.
    """
    return SubScore(
        score=NEUTRAL, direction=NONE, active=False, detail="onchain=inactive (Phase 9)"
    )


def sentiment_subscore(row: Mapping[str, object] | None = None) -> SubScore:
    """Neutral placeholder until Phase 9 wires the sentiment collector (BUILD_PLAN).

    Inactive: it neither votes in the gate nor counts toward agreement.
    """
    return SubScore(
        score=NEUTRAL, direction=NONE, active=False, detail="sentiment=inactive (Phase 9)"
    )


def _get(row: Mapping[str, object], key: str) -> object:
    """Read a column from a dict or sqlite3.Row, returning None if absent.

    sqlite3.Row has no ``.get`` and raises IndexError for an unknown column; a
    dict raises KeyError. This unifies both so callers can pass either.
    """
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _as_float(value: object) -> float | None:
    """Coerce a stored value to float, treating NULL/blank as 'missing'."""
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"
