"""Judge the *system*, not single trades: metrics + source attribution (Phase 7).

One trade tells you nothing (PLAN §5.7, FR-LE-4). This module reads the closed
``trades`` journal and computes, over the whole sample, the four numbers that decide
whether the system has an edge (FR-LE-1):

  * **win rate**            — fraction of closed trades with pnl > 0
  * **avg win / avg loss**  — the reward:risk actually realized
  * **net P&L after fees**  — the bottom line (pnl is already fee-net, FR-EX-3)
  * **max drawdown**        — worst peak-to-trough decline of realized equity

It then attributes outcomes back to the three information sources (FR-LE-3): on
losing trades, which sub-score was high — i.e. which source was confidently bullish
on trades that went on to lose? A source that scores higher on losers than winners is
*hurting*; one that scores higher on winners is *helping*. This is the data behind
the operator's hand-tuning (PLAN §5.7: "if losers consistently had strong sentiment
but weak market structure → lower the sentiment weight"). Tuning stays operator-driven
in v1 — the system informs, the operator decides (FR-LE-5).

**Sample-size honesty (FR-LE-4).** Every summary states the verdict's confidence: a
real verdict needs ≥ 100 closed trades; 30–100 is a weak preliminary read; below 30
is noise. The report never implies an edge from too few trades.

This module reads only the DB (``trades`` joined to ``signals``); it makes no trade
and changes no decision logic (FR-DP-2). It is pure analysis over stored data.
"""

from __future__ import annotations

import argparse
import logging
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

# Allow running as a plain script (python3 learning/postmortem.py) as well as a module.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402

log = logging.getLogger("postmortem")

# Confidence thresholds for a performance verdict (FR-LE-4 / PLAN §5.7). Mirrors
# core/risk.MIN_TRADES_FOR_JOURNAL_STATS so sizing and judging agree on "enough".
VERDICT_MIN_TRADES = 100   # at/above this, metrics are an actionable verdict
WEAK_READ_MIN_TRADES = 30  # 30–100 is a weak preliminary read; below 30 is noise

# The three information sources, as the ``signals`` sub-score columns they map to.
SOURCES = ("market", "onchain", "sentiment")
# A source's win/loss sub-score gap smaller than this (in 0–100 points) is treated
# as "no clear signal either way" rather than over-reading journal noise.
ATTRIBUTION_EPS = 1.0


@dataclass
class Metrics:
    """The four headline metrics over a sample of closed trades (FR-LE-1).

    ``avg_loss`` and ``max_drawdown`` are stored as positive magnitudes (a loss of
    -16.47 contributes 16.47 to ``avg_loss``). ``verdict`` is the sample-size
    confidence label, never an assertion of edge from too few trades (FR-LE-4).
    """

    n_closed: int
    n_wins: int
    n_losses: int
    win_rate: float
    avg_win: float
    avg_loss: float
    win_loss_ratio: float
    net_pnl: float
    max_drawdown: float
    max_drawdown_pct: float
    verdict: str


@dataclass
class SourceAttribution:
    """How one source's conviction split between winning and losing trades (FR-LE-3).

    ``delta = avg_on_wins - avg_on_losses``: positive means the source was more
    bullish on winners (helping); negative means it was more bullish on losers
    (hurting — a candidate to down-weight).
    """

    source: str
    avg_on_wins: float
    avg_on_losses: float
    delta: float
    verdict: str  # "helping" | "hurting" | "neutral"


def verdict_label(n_closed: int) -> str:
    """Sample-size confidence for ``n_closed`` trades (FR-LE-4)."""
    if n_closed >= VERDICT_MIN_TRADES:
        return f"actionable verdict ({n_closed} ≥ {VERDICT_MIN_TRADES} closed trades)"
    if n_closed >= WEAK_READ_MIN_TRADES:
        return f"weak preliminary read ({n_closed} of {VERDICT_MIN_TRADES} closed trades)"
    return f"noise — not a verdict ({n_closed} < {WEAK_READ_MIN_TRADES} closed trades)"


def closed_trades_with_signals(conn, mode: str = "dry", since_ts: int | None = None) -> list:
    """Closed trades for a mode, each joined to the signal that opened it.

    LEFT JOIN so a trade still counts toward metrics even if its signal row is
    missing; attribution simply skips any sub-score that is NULL. Ordered by close
    time so the equity curve (drawdown) is chronological.
    """
    sql = (
        "SELECT t.id, t.symbol, t.entry_ts, t.exit_ts, t.exit_reason, t.pnl, "
        "t.pnl_pct, t.win, s.market_sub, s.onchain_sub, s.sentiment_sub, s.composite "
        "FROM trades t LEFT JOIN signals s ON t.signal_id = s.id "
        "WHERE t.status = 'closed' AND t.mode = ? AND t.pnl IS NOT NULL"
    )
    params: list = [mode]
    if since_ts is not None:
        sql += " AND t.exit_ts >= ?"
        params.append(since_ts)
    sql += " ORDER BY t.exit_ts, t.id"
    return db.query(conn, sql, params)


def _max_drawdown(pnls: Sequence[float], bankroll: float) -> tuple[float, float]:
    """Worst peak-to-trough decline of realized equity (absolute, and as a fraction).

    Equity starts at ``bankroll`` and steps by each closed trade's fee-net P&L, in
    close order. Matches core/risk._current_drawdown so the postmortem and the risk
    gate agree on what a drawdown is.
    """
    equity = peak = bankroll
    max_abs = 0.0
    max_pct = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        drop = peak - equity
        max_abs = max(max_abs, drop)
        if peak > 0:
            max_pct = max(max_pct, drop / peak)
    return max_abs, max_pct


def compute_metrics(rows: Sequence, bankroll: float) -> Metrics:
    """The four headline metrics over ``rows`` (closed trades, close-ordered)."""
    pnls = [float(r["pnl"]) for r in rows]
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p <= 0]  # positive magnitudes

    win_rate = len(wins) / n if n else 0.0
    avg_win = statistics.fmean(wins) if wins else 0.0
    avg_loss = statistics.fmean(losses) if losses else 0.0
    if avg_loss > 0:
        win_loss_ratio = avg_win / avg_loss
    else:  # no losing trades yet — ratio is undefined; report it as 0 not infinity
        win_loss_ratio = 0.0
    net_pnl = sum(pnls)
    dd_abs, dd_pct = _max_drawdown(pnls, bankroll)

    return Metrics(
        n_closed=n,
        n_wins=len(wins),
        n_losses=len(losses),
        win_rate=win_rate,
        avg_win=avg_win,
        avg_loss=avg_loss,
        win_loss_ratio=win_loss_ratio,
        net_pnl=net_pnl,
        max_drawdown=dd_abs,
        max_drawdown_pct=dd_pct,
        verdict=verdict_label(n),
    )


def _source_verdict(delta: float) -> str:
    if delta > ATTRIBUTION_EPS:
        return "helping"
    if delta < -ATTRIBUTION_EPS:
        return "hurting"
    return "neutral"


def compute_attribution(rows: Sequence) -> list[SourceAttribution]:
    """Per-source avg conviction on winners vs losers (FR-LE-3).

    For each source, the mean of its 0–100 sub-score across winning trades and across
    losing trades. Rows whose sub-score is NULL (e.g. a placeholder source) are
    skipped for that source only, so partial data still attributes the live sources.
    """
    wins = [r for r in rows if r["win"]]
    losses = [r for r in rows if not r["win"]]
    out: list[SourceAttribution] = []
    for source in SOURCES:
        col = f"{source}_sub"
        win_scores = [float(r[col]) for r in wins if r[col] is not None]
        loss_scores = [float(r[col]) for r in losses if r[col] is not None]
        avg_w = statistics.fmean(win_scores) if win_scores else 0.0
        avg_l = statistics.fmean(loss_scores) if loss_scores else 0.0
        delta = avg_w - avg_l
        out.append(SourceAttribution(source, avg_w, avg_l, delta, _source_verdict(delta)))
    return out


def prime_suspect(attribution: Sequence[SourceAttribution]) -> SourceAttribution | None:
    """The source most worth down-weighting: the most negative win/loss delta.

    Returns None when no source is meaningfully hurting (all within ATTRIBUTION_EPS).
    """
    hurting = [a for a in attribution if a.verdict == "hurting"]
    if not hurting:
        return None
    return min(hurting, key=lambda a: a.delta)


def format_summary(
    metrics: Metrics,
    attribution: Sequence[SourceAttribution],
    *,
    mode: str,
    period_label: str,
) -> str:
    """Render the metrics + attribution into a readable weekly summary (FR-LE-2)."""
    lines: list[str] = []
    lines.append("═══ Crypto Signal Bot — performance summary ═══")
    lines.append(f"mode: {mode}   |   sample: {period_label}")
    lines.append(f"confidence: {metrics.verdict}")
    lines.append("")

    if metrics.n_closed == 0:
        lines.append("No closed trades yet — nothing to judge.")
        lines.append("")
        lines.append(_DISCLAIMER)
        return "\n".join(lines)

    lines.append(f"closed trades : {metrics.n_closed}  "
                 f"({metrics.n_wins} win / {metrics.n_losses} loss)")
    lines.append(f"win rate      : {metrics.win_rate:.1%}")
    lines.append(f"avg win       : {metrics.avg_win:+.2f}")
    lines.append(f"avg loss      : {-metrics.avg_loss:+.2f}")
    lines.append(f"win/loss ratio: {metrics.win_loss_ratio:.2f}")
    lines.append(f"net P&L (fees): {metrics.net_pnl:+.2f}")
    lines.append(f"max drawdown  : {metrics.max_drawdown:.2f} ({metrics.max_drawdown_pct:.1%})")
    lines.append("")

    lines.append("source attribution (avg sub-score on wins vs losses):")
    for a in attribution:
        lines.append(
            f"  {a.source:<9} win {a.avg_on_wins:5.1f} | loss {a.avg_on_losses:5.1f} "
            f"| Δ {a.delta:+5.1f}  → {a.verdict}"
        )
    suspect = prime_suspect(attribution)
    if suspect is not None:
        lines.append(
            f"  hint: '{suspect.source}' was more bullish on losers than winners "
            f"(Δ {suspect.delta:+.1f}) — consider lowering its weight."
        )
    else:
        lines.append("  hint: no source is clearly hurting; weights look balanced for now.")
    lines.append("")
    lines.append(_DISCLAIMER)
    return "\n".join(lines)


_DISCLAIMER = (
    "Educational software — not financial advice. Judge the system on the sample, "
    "never on a single trade. Past results do not guarantee future performance."
)


def build_report(
    conn,
    *,
    mode: str = "dry",
    days: int | None = None,
    bankroll: float = 10_000.0,
    now: int | None = None,
) -> str:
    """Compute metrics + attribution and return the formatted summary string.

    When ``days`` is given, only trades that closed within that window are included
    (a true weekly cut); otherwise the whole journal is judged (the right scope for a
    ≥100-trade verdict). ``now`` overrides the clock for deterministic tests.
    """
    since_ts = None
    period_label = "all closed trades"
    if days is not None:
        import time

        clock = now if now is not None else int(time.time())
        since_ts = clock - days * 86_400
        period_label = f"closed in the last {days} day(s)"

    rows = closed_trades_with_signals(conn, mode=mode, since_ts=since_ts)
    metrics = compute_metrics(rows, bankroll)
    attribution = compute_attribution(rows)
    return format_summary(metrics, attribution, mode=mode, period_label=period_label)


def _bankroll_from_config() -> float:
    """Read the sizing bankroll from config so drawdown % matches the risk gate."""
    from core import scoring  # reuses the one config loader (FR-CF-1/2)

    cfg = scoring.load_config()
    risk = (cfg.get("risk") or {}) if cfg else {}
    return float(risk.get("bankroll", 10_000.0))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Performance postmortem over the trade journal")
    parser.add_argument("--mode", default="dry", choices=("dry", "live"),
                        help="which journal to read (default: dry)")
    parser.add_argument("--days", type=int, default=None,
                        help="only trades closed in the last N days (default: all)")
    parser.add_argument("--telegram", action="store_true",
                        help="also send the summary via Telegram (if enabled/configured)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    conn = db.connect()
    try:
        report = build_report(conn, mode=args.mode, days=args.days,
                              bankroll=_bankroll_from_config())
    finally:
        conn.close()

    print(report)

    if args.telegram:
        from core import scoring
        from notifications.telegram import TelegramNotifier

        notifier = TelegramNotifier.from_config(scoring.load_config())
        if notifier.notify_summary(report):
            log.info("summary sent via Telegram")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
