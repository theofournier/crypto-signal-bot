"""Stage 1 of the engine: raw features -> 0-100 sub-score + direction (PLAN §5.3).

Each information source speaks a different language — a 3.2x volume ratio, an RSI
of 61, a +1.8% distance from VWAP cannot be averaged raw. Normalization squashes
each source onto a **common 0-100 sub-score** (50 = neutral) plus a **direction**
(``long`` / ``bearish`` / ``none``) so the composite stage can fuse them
arithmetically. This module is pure: it reads feature values and returns numbers,
touching neither the DB nor any decision/trade logic.

Phase 9 scope (BUILD_PLAN): all three sources are live, so all three sub-scores are
real. Each ``*_subscore`` takes the most recent (fresh) observation row for its
source and returns a 0-100 sub-score + direction; given ``None`` (no data this
cycle) it returns a neutral, **inactive** placeholder so a missing/unavailable
source neither votes nor blocks the gate (FR-DC-4 graceful degradation). The gate
(``core/scoring.py``) only requires agreement among *active* sources, so when a
collector is down the engine still decides on the sources that are fresh.

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

# ── on-chain normalization tunables ─────────────────────────────────
# ``flow_signal`` strings written by the on-chain collector (PLAN §6). Matched as
# plain strings (not imported from the collector) to keep core/ decoupled from
# collectors/ — they communicate only through the DB.
FLOW_ACCUMULATION = "accumulation"  # coins leaving exchanges  -> bullish lean
FLOW_DISTRIBUTION = "distribution"  # coins onto exchanges     -> bearish lean
FLOW_NEUTRAL = "neutral"            # inside the collector's deadband -> no direction
# A committed flow reading sits at least this far off neutral (so its 0-100 score
# carries the same direction the collector already decided), growing with intensity
# up to ONCHAIN_MAX_OFFSET at fully one-sided flow.
ONCHAIN_MIN_OFFSET = DIRECTION_BAND  # 10 -> score 60/40, just commits a direction
ONCHAIN_MAX_OFFSET = 40.0            # full tilt -> score 90/10
# When a source reports a direction but no scalable magnitude (a TVL-momentum source
# leaves inflow/outflow NULL), use this middle intensity rather than guessing.
ONCHAIN_DEFAULT_INTENSITY = 0.5

# ── sentiment normalization tunables ────────────────────────────────
# sentiment_score is already directional in [-1, +1]; map +/-1 onto the full 0-100
# range around neutral (+1 -> 100, -1 -> 0) before confidence damping.
SENTIMENT_SENSITIVITY = 50.0
# Low-credibility / recycled (low-novelty) chatter is damped toward neutral rather
# than trusted at face value. novelty contributes through this floor — recycled
# content keeps at least this share of its weight (and a source that omits novelty
# is not penalised for it).
NOVELTY_FLOOR = 0.5


@dataclass
class SubScore:
    """One source's normalized reading.

    ``name`` is the source id (``market``/``onchain``/``sentiment``) so the gate's
    reason can name which source disagreed. ``score`` is 0-100 (50 neutral).
    ``direction`` is long/bearish/none. ``active`` is False for a source with no
    data this cycle, which should not vote in the gate nor count toward agreement.
    ``detail`` is a short human-readable trace that feeds the signal's ``reason``
    (FR-SG-3).
    """

    name: str
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
    return SubScore(name="market", score=score, direction=direction, active=True, detail=detail)


def onchain_subscore(row: Mapping[str, object] | None = None) -> SubScore:
    """Turn one ``onchain_data`` row into a 0-100 sub-score + direction (PLAN §5.3).

    Direction follows the collector's own ``flow_signal`` (it already applied a
    deadband): accumulation -> long (coins leaving exchanges), distribution ->
    bearish (coins moving onto exchanges), neutral -> none. The score's distance
    from neutral encodes conviction — the share of one-sided flow
    (``|net_flow| / gross``) when the source measures both legs, or a middle
    intensity when it reports only a direction (e.g. a TVL-momentum source that
    leaves ``exchange_inflow``/``outflow`` NULL).

    ``None`` (no on-chain row this cycle) returns an **inactive** neutral
    placeholder so a missing/unavailable source neither votes nor blocks the gate
    (FR-DC-4); a real row is **active** and counts toward the agreement requirement
    — an on-chain reading that is present but *neutral* therefore does not agree
    long and (correctly) vetoes a long entry.
    """
    if row is None:
        return SubScore("onchain", NEUTRAL, NONE, active=False, detail="onchain=inactive (no data)")

    flow_signal = _as_text(_get(row, "flow_signal"))
    net_flow = _as_float(_get(row, "net_flow"))
    inflow = _as_float(_get(row, "exchange_inflow"))
    outflow = _as_float(_get(row, "exchange_outflow"))

    if flow_signal is None and net_flow is None:
        # A row with no directional content at all — nothing to read; stay inactive.
        return SubScore("onchain", NEUTRAL, NONE, active=False, detail="onchain=inactive (empty row)")

    direction = _onchain_direction(flow_signal, net_flow)

    # Intensity: how one-sided the flow is. Use both legs when present; otherwise a
    # source that reports only a direction gets a middle, honest intensity.
    gross = (inflow or 0.0) + (outflow or 0.0)
    if gross > 0 and net_flow is not None:
        intensity = _clamp(abs(net_flow) / gross, 0.0, 1.0)
    else:
        intensity = ONCHAIN_DEFAULT_INTENSITY

    offset = ONCHAIN_MIN_OFFSET + intensity * (ONCHAIN_MAX_OFFSET - ONCHAIN_MIN_OFFSET)
    if direction == LONG:
        score = _clamp(NEUTRAL + offset, 0.0, 100.0)
    elif direction == BEARISH:
        score = _clamp(NEUTRAL - offset, 0.0, 100.0)
    else:
        score = NEUTRAL

    detail = (
        f"onchain={score:.1f} ({direction}): "
        f"flow={flow_signal or 'n/a'}, net_flow={_fmt(net_flow)}, intensity={intensity:.2f}"
    )
    return SubScore(name="onchain", score=score, direction=direction, active=True, detail=detail)


def sentiment_subscore(row: Mapping[str, object] | None = None) -> SubScore:
    """Turn one ``sentiment_data`` row into a 0-100 sub-score + direction (PLAN §5.3).

    ``sentiment_score`` is already directional in [-1, +1]; it maps linearly onto
    0-100 around neutral. The reading is then damped toward neutral by the source's
    ``credibility`` and, when measured, ``novelty`` (fresh chatter is trusted more
    than recycled), so noisy/low-trust sentiment moves the score less. Direction
    falls out of the damped score via the shared neutral band, so weak sentiment
    reads as ``none`` rather than a fabricated lean.

    ``None`` (no sentiment row this cycle) returns an **inactive** neutral
    placeholder so a missing/unavailable source neither votes nor blocks (FR-DC-4);
    a real row is **active** and counts toward agreement.
    """
    if row is None:
        return SubScore("sentiment", NEUTRAL, NONE, active=False, detail="sentiment=inactive (no data)")

    sentiment = _as_float(_get(row, "sentiment_score"))
    if sentiment is None:
        return SubScore("sentiment", NEUTRAL, NONE, active=False, detail="sentiment=inactive (empty row)")

    credibility = _as_float(_get(row, "credibility"))
    novelty = _as_float(_get(row, "novelty"))

    base = NEUTRAL + _clamp(sentiment, -1.0, 1.0) * SENTIMENT_SENSITIVITY
    # Confidence damping: trust scales with credibility and (when present) novelty;
    # a missing factor is treated as full weight (no penalty for not measuring it).
    confidence = credibility if credibility is not None else 1.0
    if novelty is not None:
        confidence *= NOVELTY_FLOOR + (1.0 - NOVELTY_FLOOR) * _clamp(novelty, 0.0, 1.0)
    confidence = _clamp(confidence, 0.0, 1.0)
    score = _clamp(NEUTRAL + (base - NEUTRAL) * confidence, 0.0, 100.0)

    direction = _direction_for(score)
    detail = (
        f"sentiment={score:.1f} ({direction}): "
        f"score={_fmt(sentiment)}, cred={_fmt(credibility)}, novelty={_fmt(novelty)}"
    )
    return SubScore(name="sentiment", score=score, direction=direction, active=True, detail=detail)


def _onchain_direction(flow_signal: str | None, net_flow: float | None) -> str:
    """Map a flow reading to a direction (collector's ``flow_signal`` wins).

    The collector already applied its deadband to produce ``flow_signal``, so trust
    it. If the text is missing/unknown, fall back to the sign of ``net_flow`` (no
    deadband available, so any nonzero flow commits a direction).
    """
    if flow_signal == FLOW_ACCUMULATION:
        return LONG
    if flow_signal == FLOW_DISTRIBUTION:
        return BEARISH
    if flow_signal == FLOW_NEUTRAL:
        return NONE
    if net_flow is None or net_flow == 0:
        return NONE
    return LONG if net_flow > 0 else BEARISH


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


def _as_text(value: object) -> str | None:
    """Coerce a stored value to a lowercased, stripped string ('' / NULL -> None)."""
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"
