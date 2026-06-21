"""Stage 2 of the engine: composite + gate (PLAN §5.3).

Fuse the three normalized sub-scores into ONE conviction number and ONE direction,
then apply the gate. The gate fires only when **both** conditions hold (FR-SG-2):

  1. the weighted composite meets or exceeds the configured threshold, AND
  2. all **active** sources agree on ``long``.

v1 is long-or-flat, spot only (PLAN §1, FR-EX-4): the only actionable outcome is
``long``; a bearish or split reading produces ``none`` (no entry), never a short.

Every evaluation — firing or not — yields an ``Evaluation`` carrying the sub-scores,
the composite, the decision, and a human-readable ``reason`` (FR-SG-3/4, FR-DP-1).
The engine persists each one to the ``signals`` table so non-action is auditable too.

This module makes the *decision*; it does not place trades and does not touch the
exchange (that is Phase 5, ``core/risk.py`` + ``execution/``). It reads its weights,
threshold and agreement switch from ``config.yaml`` (FR-CF-1), falling back to the
committed ``config.example.yaml`` so it works out of the box (FR-CF-2).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from core import normalize
from core.normalize import LONG, NONE, SubScore

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

# Used only if config omits a value (config should normally supply all three).
DEFAULT_WEIGHTS = {"market": 0.40, "onchain": 0.25, "sentiment": 0.35}
DEFAULT_THRESHOLD = 72.0
DEFAULT_REQUIRE_AGREEMENT = True


@dataclass
class ScoringConfig:
    """The scoring knobs read from ``config.yaml`` (PLAN §9 ``scoring``)."""

    weights: dict[str, float]
    threshold: float
    require_agreement: bool


@dataclass
class Evaluation:
    """The full result of scoring one asset at one point in time.

    Maps 1:1 onto a ``signals`` row; the engine writes it verbatim so any decision
    is reconstructable after the fact (FR-DP-3, G3).
    """

    ts: int
    symbol: str
    market_sub: float
    onchain_sub: float
    sentiment_sub: float
    composite: float
    direction: str
    gate_passed: bool
    reason: str

    def as_row(self) -> dict[str, object]:
        """Shape into a ``signals`` insert (gate_passed stored as 0/1 INTEGER)."""
        return {
            "ts": self.ts,
            "symbol": self.symbol,
            "market_sub": self.market_sub,
            "onchain_sub": self.onchain_sub,
            "sentiment_sub": self.sentiment_sub,
            "composite": self.composite,
            "direction": self.direction,
            "gate_passed": 1 if self.gate_passed else 0,
            "reason": self.reason,
        }


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively overlay ``override`` onto ``base`` (override wins at every leaf).

    Nested dicts merge key-by-key so ``config.yaml`` need only carry the values it
    changes; everything else falls through to the ``base`` (example) defaults. A
    non-dict override (including a list) replaces the base value wholesale.
    """
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: str | Path | None = None) -> dict:
    """Load config, layering ``config.yaml`` over the ``config.example.yaml`` base.

    The committed example is the **defaults** layer; the operator's gitignored
    ``config.yaml`` only needs to carry the values it overrides — any key it omits
    falls back to the example (FR-CF-1/2). This means adding a new setting to the
    example template makes it available to everyone without each operator having to
    copy it into their private ``config.yaml``.

    An explicit ``path`` (used by tests) is loaded as-is, with no layering. PyYAML
    is imported lazily — only a real run needs it.
    """
    import yaml  # local import: only the engine runner needs PyYAML

    if path is not None:
        return yaml.safe_load(Path(path).read_text()) or {}

    example = yaml.safe_load((CONFIG_DIR / "config.example.yaml").read_text()) or {}
    override_path = CONFIG_DIR / "config.yaml"
    if not override_path.exists():
        return example
    override = yaml.safe_load(override_path.read_text()) or {}
    return _deep_merge(example, override)


def scoring_config(cfg: dict) -> ScoringConfig:
    """Extract the ``scoring`` section into a typed config (with safe defaults)."""
    scoring = cfg.get("scoring", {}) or {}
    weights = {**DEFAULT_WEIGHTS, **(scoring.get("weights") or {})}
    return ScoringConfig(
        weights={k: float(v) for k, v in weights.items()},
        threshold=float(scoring.get("threshold", DEFAULT_THRESHOLD)),
        require_agreement=bool(scoring.get("require_agreement", DEFAULT_REQUIRE_AGREEMENT)),
    )


def evaluate(
    market_row: Mapping[str, object],
    cfg: ScoringConfig,
    onchain_row: Mapping[str, object] | None = None,
    sentiment_row: Mapping[str, object] | None = None,
) -> Evaluation:
    """Score one asset's latest reading and apply the gate (PLAN §5.3).

    ``market_row`` is the most recent **closed** ``market_data`` row for the asset
    (FR-DC-6: the engine only ever sees completed candles). On-chain and sentiment
    rows are accepted for forward compatibility but are placeholders in Phase 4.
    """
    market = normalize.market_subscore(market_row)
    onchain = normalize.onchain_subscore(onchain_row)
    sentiment = normalize.sentiment_subscore(sentiment_row)

    # Composite: fixed weighting per PLAN §5.3 (market highest, sentiment lowest).
    composite = (
        cfg.weights["market"] * market.score
        + cfg.weights["onchain"] * onchain.score
        + cfg.weights["sentiment"] * sentiment.score
    )

    # Agreement is judged among ACTIVE sources only. In Phase 4 only the market is
    # active, so this is trivially the market direction (BUILD_PLAN Phase 4 note);
    # in Phase 9 on-chain + sentiment join and the unanimity requirement bites.
    active = [s for s in (market, onchain, sentiment) if s.active]
    agree_long = bool(active) and all(s.direction == LONG for s in active)

    above_threshold = composite >= cfg.threshold
    gate_passed = above_threshold and (agree_long or not cfg.require_agreement)
    # Long-or-flat: we only ever emit "long". Anything else is no-action.
    direction = LONG if gate_passed else NONE

    reason = _reason(composite, cfg, above_threshold, agree_long, active, market)
    return Evaluation(
        ts=int(market_row["ts"]),  # type: ignore[arg-type]
        symbol=str(market_row["symbol"]),
        market_sub=market.score,
        onchain_sub=onchain.score,
        sentiment_sub=sentiment.score,
        composite=composite,
        direction=direction,
        gate_passed=gate_passed,
        reason=reason,
    )


def _reason(
    composite: float,
    cfg: ScoringConfig,
    above_threshold: bool,
    agree_long: bool,
    active: list[SubScore],
    market: SubScore,
) -> str:
    """Build the human-readable explanation stored on every signal (FR-SG-3/4).

    States the verdict, the numbers behind it, and — when it does not fire — the
    specific reason, so the operator can reconstruct any decision (G3).
    """
    head = f"composite {composite:.1f} (threshold {cfg.threshold:.0f})"
    votes = "; ".join(s.detail for s in active) or "no active sources"

    if above_threshold and (agree_long or not cfg.require_agreement):
        return f"FIRED long: {head} cleared, active sources agree long. [{votes}]"

    blockers: list[str] = []
    if not above_threshold:
        blockers.append(f"composite {composite:.1f} < threshold {cfg.threshold:.0f}")
    if cfg.require_agreement and not agree_long:
        blockers.append(f"active sources do not all agree long (market={market.direction})")
    return f"no signal: {'; '.join(blockers)}. [{votes}]"
