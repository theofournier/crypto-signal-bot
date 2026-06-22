"""Live sentiment collector (BUILD_PLAN Phase 9).

Pulls social/news chatter about an asset (Reddit / Telegram / RSS) and the
market-wide Fear & Greed mood, runs each text item through a **classifier**, and
reduces the batch to one rolling row in ``sentiment_data`` (PLAN §5.1, §6): a
directional ``sentiment_score`` (−1 bearish … +1 bullish) with a source
``credibility``, a ``novelty`` indicator, and the ``mention_count`` over the
window. It writes a *rolling aggregate, never individual posts* (PLAN §5.1).

Like every collector this is the base ``fetch -> normalize -> write`` shape and
obeys the two non-negotiables (PLAN §2):

  * **Observe, never decide** (FR-DC-5). It only writes rows; there is no
    scoring/trading logic here. Turning these rows into a sub-score happens later,
    in ``core/normalize.py`` (a separate Phase 9 task).
  * **Sources operate independently / degrade gracefully** (FR-DC-4, NFR-REL). A
    missing dependency (``praw``/``telethon``/``feedparser``), an absent API key,
    or a transient outage yields an empty cycle (nothing written) — never a crash,
    and never fabricated sentiment.

**Pluggable, composable sources + a pluggable classifier.** Both the data sources
and the text→score classifier are injected (mirroring how ``onchain_collector``
injects its source), so the collector is testable without a network and pieces
swap without touching this loop. Four real sources ship, and unlike the on-chain
"first to cover wins" routing they are **blended** (:class:`BlendSource`) —
sentiment genuinely benefits from combining independent feeds, weighted by each
source's credibility:

  * :class:`FearGreedSource` — the market-wide **Fear & Greed Index**
    (alternative.me), free and keyless. Always available, so the collector
    produces a meaningful reading out of the box even with no social keys set.
  * :class:`RssSource` — crypto **news** headlines (feedparser), free and keyless,
    filtered to items that mention the asset.
  * :class:`RedditSource` — **Reddit** posts via PRAW (needs ``REDDIT_*`` keys).
  * :class:`TelegramSource` — **Telegram** channel messages via Telethon (needs
    ``TELEGRAM_*`` creds + a saved session string).

**The LLM/classifier lives here, not in scoring** (PLAN §5.3). The default
:class:`LexiconClassifier` is a transparent, dependency-free rule-based scorer;
:class:`OllamaClassifier` swaps in a **local LLM** via Ollama (free, self-hosted),
always wrapped in a :class:`FallbackClassifier` so a stopped server degrades to the
lexicon rather than halting the collector. Any other model (CryptoBERT, or one
trained on a Kaggle dataset — see ``scripts/seed_sentiment.py``) drops in by
implementing :class:`SentimentClassifier`. Scoring downstream only ever sees the
numbers.

**Config-driven, honest coverage.** Sources, feeds, subreddits, channels, the
classifier lexicon, and per-source credibility all read from the ``sentiment``
config block — add/adjust with **no code change**; built-in defaults are used when
config omits them. Each source fills only the columns it actually measures (the
Fear & Greed index has no per-item ``novelty``/``mention_count`` and leaves them
``NULL`` — honest, not faked).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from collectors.base_collector import BaseCollector

# ── tunable defaults (overridable via config / constructor) ─────────
DEFAULT_POLL_SECONDS = 600         # slow: social/news APIs rate-limit, mood drifts slowly
DEFAULT_WINDOW_SECONDS = 6 * 3600  # aggregate chatter over a trailing 6h window
DEFAULT_HTTP_TIMEOUT = 15.0


# ── one raw item of chatter ─────────────────────────────────────────
@dataclass(frozen=True)
class SentimentItem:
    """One social/news item mentioning an asset, as raw text (scored later).

    ``credibility`` is the source's trust prior (0–1); ``ident`` is a stable id
    (post id / link) used to measure novelty (distinct vs recycled content).
    """

    ts: int
    text: str
    source: str       # "reddit" | "telegram" | "rss"
    credibility: float
    ident: str


# ── classifier: text -> directional sentiment ───────────────────────
class SentimentClassifier(ABC):
    """Turns one text item into a directional score in ``[-1, +1]``.

    The classifier is the only place natural language becomes a number — keep it
    here, upstream of scoring (PLAN §5.3). Implement and inject your own (e.g. a
    local CryptoBERT via Ollama) to replace the rule-based default.
    """

    @abstractmethod
    def score(self, text: str) -> float | None:
        """Sentiment of ``text`` in ``[-1, +1]``, or ``None`` if undecidable
        (no signal-bearing words) — an undecidable item is dropped, not counted."""


# Default bull/bear lexicons. Single lowercase tokens; extend via config
# (``sentiment.classifier.extra_bullish`` / ``extra_bearish``) — no code change.
DEFAULT_BULLISH_TERMS = frozenset({
    "bull", "bullish", "moon", "mooning", "pump", "pumping", "surge", "surging",
    "rally", "rallying", "breakout", "ath", "long", "buy", "buying", "accumulate",
    "accumulating", "gain", "gains", "soar", "soaring", "rip", "green", "support",
    "adoption", "partnership", "upgrade", "rocket", "uptrend",
})
DEFAULT_BEARISH_TERMS = frozenset({
    "bear", "bearish", "dump", "dumping", "crash", "crashing", "plunge", "plunging",
    "rug", "rugpull", "scam", "fear", "fud", "sell", "selling", "short", "shorting",
    "drop", "dropping", "fall", "falling", "red", "liquidated", "liquidation",
    "hack", "exploit", "ban", "lawsuit", "correction", "downtrend", "weak",
})

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    """Lowercase word/number tokens (pure) — the unit both scoring and relevance use."""
    return _TOKEN_RE.findall(text.lower())


class LexiconClassifier(SentimentClassifier):
    """Transparent rule-based scorer: ``(bull − bear) / (bull + bear)`` (pure).

    Counts bullish vs bearish keyword hits and returns their normalized balance in
    ``[-1, +1]``. ``None`` when a text carries no lexicon word (so neutral chatter
    neither votes nor dilutes). No dependencies, no training, fully unit-testable —
    the honest baseline a learned model must beat before it earns its place.
    """

    def __init__(
        self,
        bullish: frozenset[str] = DEFAULT_BULLISH_TERMS,
        bearish: frozenset[str] = DEFAULT_BEARISH_TERMS,
    ) -> None:
        self.bullish = bullish
        self.bearish = bearish

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | None) -> "LexiconClassifier":
        """Build from ``sentiment.classifier`` (adds extra_bullish/extra_bearish)."""
        cfg = cfg or {}
        bullish = DEFAULT_BULLISH_TERMS | {str(w).lower() for w in (cfg.get("extra_bullish") or [])}
        bearish = DEFAULT_BEARISH_TERMS | {str(w).lower() for w in (cfg.get("extra_bearish") or [])}
        return cls(bullish=bullish, bearish=bearish)

    def score(self, text: str) -> float | None:
        bull = bear = 0
        for tok in _tokenize(text):
            if tok in self.bullish:
                bull += 1
            elif tok in self.bearish:
                bear += 1
        total = bull + bear
        if total == 0:
            return None  # no signal-bearing words → undecidable, drop it
        return (bull - bear) / total


# ── local-LLM classifier via Ollama (PLAN §5.1, §11) ────────────────
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "llama3.2:3b"   # any model you've `ollama pull`ed
DEFAULT_OLLAMA_TIMEOUT = 30.0
DEFAULT_OLLAMA_CACHE = 2048

# Deterministic, output-constrained prompt: ask for one number in [-1, 1] so the
# response parses cleanly. ``temperature: 0`` (set on the request) makes a given
# text score reproducibly — essential for an auditable, non-repainting pipeline.
OLLAMA_PROMPT_TEMPLATE = (
    "You are a financial sentiment classifier for cryptocurrency markets.\n"
    "Rate the sentiment of the text below toward the asset's near-term price.\n"
    "Respond with ONLY a single number from -1.0 to 1.0 and nothing else:\n"
    "  -1.0 = very bearish, 0.0 = neutral, 1.0 = very bullish.\n\n"
    "Text: {text}\n"
    "Score:"
)

_SCORE_RE = re.compile(r"-?\d+(?:\.\d+)?")


def build_ollama_prompt(text: str) -> str:
    """Build the deterministic classification prompt for one item (pure)."""
    return OLLAMA_PROMPT_TEMPLATE.format(text=text.strip())


def parse_ollama_score(response: str) -> float | None:
    """Parse a model response into a clamped ``[-1, +1]`` score (pure).

    Accepts a bare number (preferred) and falls back to bullish/bearish/neutral
    words, so a chatty model still yields a usable verdict. ``None`` when nothing
    parseable is found — treated as "undecidable", exactly like a neutral lexicon
    item (validate on intake — PLAN §7).
    """
    text = (response or "").strip().lower()
    match = _SCORE_RE.search(text)
    if match:
        try:
            return _clamp(float(match.group()), -1.0, 1.0)
        except ValueError:
            pass
    if "bullish" in text:
        return 1.0
    if "bearish" in text:
        return -1.0
    if "neutral" in text:
        return 0.0
    return None


class OllamaUnavailable(RuntimeError):
    """The local Ollama server could not be reached or returned an error.

    Raised (not returned as ``None``) so callers can tell a transport failure apart
    from a genuine "model couldn't decide" — :class:`FallbackClassifier` catches
    this to drop back to the rule-based baseline (FR-DC-4).
    """


class OllamaClassifier(SentimentClassifier):
    """Score items with a **local LLM** served by Ollama (free, self-hosted).

    Each item is sent to Ollama's ``/api/generate`` with a constrained,
    ``temperature: 0`` prompt and parsed to a number — the LLM lives here in the
    collector, upstream of scoring (PLAN §5.3), never in the scoring step itself.
    Verdicts are cached by normalized text (recurring posts cost one call, not
    many). A transport/HTTP failure raises :class:`OllamaUnavailable` so a wrapper
    can fall back; wrap with :class:`FallbackClassifier` to keep the rules baseline
    always available. ``urllib`` is the only non-pure part; prompt building and
    response parsing stay in the pure helpers above.
    """

    def __init__(
        self,
        model: str = DEFAULT_OLLAMA_MODEL,
        base_url: str = DEFAULT_OLLAMA_URL,
        timeout: float = DEFAULT_OLLAMA_TIMEOUT,
        cache_size: int = DEFAULT_OLLAMA_CACHE,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.cache_size = cache_size
        self._cache: "OrderedDict[str, float | None]" = OrderedDict()
        self.log = logging.getLogger("collector.sentiment.ollama")

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | None) -> "OllamaClassifier":
        """Build from ``sentiment.classifier.ollama`` (uses defaults if absent)."""
        cfg = cfg or {}
        return cls(
            model=str(cfg.get("model", DEFAULT_OLLAMA_MODEL)),
            base_url=str(cfg.get("base_url", DEFAULT_OLLAMA_URL)),
            timeout=float(cfg.get("timeout", DEFAULT_OLLAMA_TIMEOUT)),
            cache_size=int(cfg.get("cache_size", DEFAULT_OLLAMA_CACHE)),
        )

    def score(self, text: str) -> float | None:
        key = _normalize_text(text)
        cached = self._cache.get(key, _MISS)
        if cached is not _MISS:
            self._cache.move_to_end(key)
            return cached  # type: ignore[return-value]
        value = parse_ollama_score(self._generate(build_ollama_prompt(text)))
        self._remember(key, value)
        return value

    def _remember(self, key: str, value: float | None) -> None:
        """Cache a verdict, evicting the least-recently-used over capacity."""
        self._cache[key] = value
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)

    def _generate(self, prompt: str) -> str:
        """One Ollama ``/api/generate`` call; raises :class:`OllamaUnavailable`."""
        body = json.dumps({
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0},
        }).encode()
        request = urllib.request.Request(
            f"{self.base_url}/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:  # noqa: S310 - configured host
                payload = json.loads(resp.read().decode())
        except Exception as exc:  # noqa: BLE001 — surface any transport/parse error uniformly
            raise OllamaUnavailable(f"ollama request failed: {exc}") from exc
        return str(payload.get("response", ""))


class FallbackClassifier(SentimentClassifier):
    """Try a primary classifier; fall back to an always-available one on failure.

    Mirrors the project's rules-as-baseline philosophy (a learned model never
    becomes a single point of failure): when the primary (e.g.
    :class:`OllamaClassifier`) raises, the rule-based :class:`LexiconClassifier`
    keeps the collector producing data (FR-DC-4). A primary that *returns* ``None``
    (a genuine "undecidable") is respected — only an exception triggers fallback.
    """

    def __init__(self, primary: SentimentClassifier, fallback: SentimentClassifier) -> None:
        self.primary = primary
        self.fallback = fallback
        self.log = logging.getLogger("collector.sentiment.classifier")
        self._warned = False

    def score(self, text: str) -> float | None:
        try:
            value = self.primary.score(text)
            self._warned = False  # primary healthy again
            return value
        except Exception as exc:  # noqa: BLE001 — any primary failure → degrade, never crash
            if not self._warned:  # warn once per outage, not once per item
                self.log.warning("primary classifier unavailable (%s); using lexicon fallback", exc)
                self._warned = True
            return self.fallback.score(text)


# Sentinel for "cache miss" so a cached ``None`` (genuine undecidable) is not
# mistaken for "not cached" and re-queried every cycle.
_MISS = object()


# ── reduction (pure): a batch of items -> one sentiment_data reading ─
def aggregate_items(
    items: Sequence[SentimentItem], classifier: SentimentClassifier
) -> dict[str, Any] | None:
    """Reduce classified items to a rolling sentiment reading (pure, no I/O).

      * sentiment_score = credibility-weighted mean of item scores (−1…+1)
      * credibility     = mean item credibility (0–1)
      * novelty         = distinct texts ÷ items (1 = all unique, low = recycled)
      * mention_count   = how many items carried a usable signal
      * source          = the lone source's name, or "mixed"

    Returns ``None`` when no item carried a usable signal (nothing to write —
    a normal, non-error empty cycle, FR-DC-4).
    """
    scored: list[tuple[SentimentItem, float]] = []
    for it in items:
        s = classifier.score(it.text)
        if s is None:
            continue
        scored.append((it, _clamp(s, -1.0, 1.0)))
    if not scored:
        return None

    weight_sum = sum(it.credibility for it, _ in scored)
    if weight_sum > 0:
        sentiment_score = sum(s * it.credibility for it, s in scored) / weight_sum
    else:  # all-zero credibility → fall back to a plain mean rather than divide by 0
        sentiment_score = sum(s for _, s in scored) / len(scored)

    credibility = sum(it.credibility for it, _ in scored) / len(scored)
    distinct = len({_normalize_text(it.text) for it, _ in scored})
    novelty = distinct / len(scored)
    sources = {it.source for it, _ in scored}
    source = next(iter(sources)) if len(sources) == 1 else "mixed"

    return {
        "sentiment_score": sentiment_score,
        "credibility": credibility,
        "novelty": novelty,
        "mention_count": len(scored),
        "source": source,
    }


def blend_readings(readings: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Blend several sources' readings into one, weighted by credibility (pure).

    Sentiment improves by combining independent feeds (news + social + the F&G
    mood), so unlike the on-chain "first source wins" routing we merge: each
    reading's pull on the blended ``sentiment_score`` is its credibility.
    ``mention_count`` sums; ``novelty`` is a credibility-weighted mean of the
    sources that measured it; absent fields are skipped honestly.
    """
    readings = [r for r in readings if r is not None]
    if not readings:
        return None

    def cred(r: Mapping[str, Any]) -> float:
        c = r.get("credibility")
        return 1.0 if c is None else float(c)

    weight_sum = sum(cred(r) for r in readings)
    if weight_sum > 0:
        sentiment_score = sum(float(r["sentiment_score"]) * cred(r) for r in readings) / weight_sum
    else:
        sentiment_score = sum(float(r["sentiment_score"]) for r in readings) / len(readings)

    creds = [cred(r) for r in readings]
    credibility = sum(creds) / len(creds)

    mentions = [int(r["mention_count"]) for r in readings if r.get("mention_count") is not None]
    mention_count = sum(mentions) if mentions else None

    nov = [(float(r["novelty"]), cred(r)) for r in readings if r.get("novelty") is not None]
    nov_w = sum(w for _, w in nov)
    novelty = (sum(n * w for n, w in nov) / nov_w) if nov_w > 0 else None

    sources = {r.get("source") for r in readings}
    source = next(iter(sources)) if len(sources) == 1 else "mixed"

    return {
        "sentiment_score": _clamp(sentiment_score, -1.0, 1.0),
        "credibility": credibility,
        "novelty": novelty,
        "mention_count": mention_count,
        "source": source,
    }


# ── sources ─────────────────────────────────────────────────────────
class SentimentSource(ABC):
    """A pluggable sentiment provider. Implement and inject your own.

    A source answers two questions: does it **cover** an asset, and (if so) what is
    the asset's sentiment **reading** over a window? ``None`` from ``read`` means
    "no data this cycle" (uncovered, unavailable, or a transient gap) — the
    collector then writes nothing, degrading gracefully (FR-DC-4).
    """

    @abstractmethod
    def covers(self, base_asset: str) -> bool:
        """True if this source can produce a reading for ``base_asset``."""

    @abstractmethod
    def read(self, base_asset: str, since_ts: int, now_ts: int) -> dict | None:
        """The sentiment reading (``sentiment_data`` columns) over the window, or
        ``None`` if unavailable."""


class TextSource(SentimentSource):
    """Base for sources that observe **text items** (Reddit, Telegram, RSS).

    Subclasses implement :meth:`fetch_items` (raw items mentioning the asset in the
    window); this base classifies and reduces them to a reading via
    :func:`aggregate_items`. ``covers`` is True by default — text sources find an
    asset by keyword, so they can speak to any asset (relevant items are filtered
    inside :meth:`fetch_items`).
    """

    def __init__(self, classifier: SentimentClassifier) -> None:
        self.classifier = classifier

    def covers(self, base_asset: str) -> bool:  # noqa: ARG002 - any asset by keyword
        return True

    @abstractmethod
    def fetch_items(
        self, base_asset: str, since_ts: int, now_ts: int
    ) -> list[SentimentItem] | None:
        """Items mentioning ``base_asset`` in ``[since_ts, now_ts]``.

        Returns ``None`` when the source is unavailable (missing dep/keys/outage),
        vs ``[]`` = "available, but no relevant chatter in the window".
        """

    def read(self, base_asset: str, since_ts: int, now_ts: int) -> dict | None:
        items = self.fetch_items(base_asset, since_ts, now_ts)
        if items is None:
            return None  # unavailable this cycle
        return aggregate_items(items, self.classifier)


# Per-asset keyword aliases for relevance filtering of general crypto feeds.
# Edit/extend via config (``sentiment.aliases``); unknown assets fall back to the
# lowercased base symbol (e.g. "FOO" -> ("foo",)).
DEFAULT_ALIASES: dict[str, tuple[str, ...]] = {
    "BTC": ("btc", "bitcoin"),
    "ETH": ("eth", "ethereum", "ether"),
    "SOL": ("sol", "solana"),
    "AVAX": ("avax", "avalanche"),
    "SUI": ("sui",),
    "NEAR": ("near",),
    "LINK": ("link", "chainlink"),
    "INJ": ("inj", "injective"),
    "DOGE": ("doge", "dogecoin"),
    "TIA": ("tia", "celestia"),
}


def _aliases_for(base_asset: str, aliases: Mapping[str, Sequence[str]]) -> tuple[str, ...]:
    key = base_asset.upper()
    if key in aliases:
        return tuple(str(a).lower() for a in aliases[key])
    return (base_asset.lower(),)


def mentions(text: str, aliases: Sequence[str]) -> bool:
    """True if ``text`` mentions any alias as a whole token (pure, case-insensitive)."""
    tokens = set(_tokenize(text))
    return any(a in tokens for a in aliases)


DEFAULT_RSS_FEEDS: tuple[str, ...] = (
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
)
DEFAULT_RSS_CREDIBILITY = 0.5


class RssSource(TextSource):
    """Crypto **news** headlines via RSS (feedparser): free, keyless.

    Parses each configured feed, keeps entries inside the window that mention the
    asset, and emits one :class:`SentimentItem` per entry. ``feedparser`` is the
    only non-pure part and is imported lazily — its absence degrades the source to
    "unavailable" (``None``) rather than crashing (FR-DC-4).
    """

    def __init__(
        self,
        classifier: SentimentClassifier,
        feeds: Sequence[str] | None = None,
        aliases: Mapping[str, Sequence[str]] | None = None,
        credibility: float = DEFAULT_RSS_CREDIBILITY,
    ) -> None:
        super().__init__(classifier)
        self.feeds = tuple(feeds) if feeds is not None else DEFAULT_RSS_FEEDS
        self.aliases = aliases if aliases is not None else DEFAULT_ALIASES
        self.credibility = credibility
        self.log = logging.getLogger("collector.sentiment.rss")

    @classmethod
    def from_config(
        cls, cfg: Mapping[str, Any] | None, classifier: SentimentClassifier,
        aliases: Mapping[str, Sequence[str]] | None = None,
    ) -> "RssSource":
        cfg = cfg or {}
        return cls(
            classifier,
            feeds=cfg.get("feeds"),
            aliases=aliases,
            credibility=float(cfg.get("credibility", DEFAULT_RSS_CREDIBILITY)),
        )

    def fetch_items(
        self, base_asset: str, since_ts: int, now_ts: int
    ) -> list[SentimentItem] | None:
        try:
            import feedparser  # noqa: PLC0415 — lazy, optional dependency
        except ImportError:
            self.log.warning("feedparser not installed; RSS source unavailable")
            return None

        aliases = _aliases_for(base_asset, self.aliases)
        items: list[SentimentItem] = []
        for url in self.feeds:
            try:
                parsed = feedparser.parse(url)  # network + parse
            except Exception:  # noqa: BLE001 — one feed must not sink the rest
                self.log.exception("rss parse failed (%s); skipping", url)
                continue
            items.extend(
                _rss_entries_to_items(
                    getattr(parsed, "entries", []), aliases, since_ts, now_ts, self.credibility
                )
            )
        return items


def _rss_entries_to_items(
    entries: Sequence[Mapping[str, Any]],
    aliases: Sequence[str],
    since_ts: int,
    now_ts: int,
    credibility: float,
) -> list[SentimentItem]:
    """Filter parsed RSS entries to in-window, asset-relevant items (pure)."""
    out: list[SentimentItem] = []
    for entry in entries:
        ts = _rss_entry_ts(entry)
        if ts is None or ts < since_ts or ts > now_ts:
            continue
        text = f"{entry.get('title', '')} {entry.get('summary', '')}".strip()
        if not text or not mentions(text, aliases):
            continue
        ident = str(entry.get("id") or entry.get("link") or text)
        out.append(SentimentItem(ts=ts, text=text, source="rss", credibility=credibility, ident=ident))
    return out


def _rss_entry_ts(entry: Mapping[str, Any]) -> int | None:
    """Unix seconds from a feedparser entry's parsed time, or ``None`` (pure)."""
    import calendar  # noqa: PLC0415 — stdlib, only needed when parsing entries

    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    try:
        return calendar.timegm(parsed)
    except (TypeError, ValueError):
        return None


DEFAULT_SUBREDDITS: tuple[str, ...] = ("CryptoCurrency", "CryptoMarkets")
DEFAULT_REDDIT_CREDIBILITY = 0.5
DEFAULT_REDDIT_LIMIT = 50


class RedditSource(TextSource):
    """**Reddit** posts via PRAW. Needs ``REDDIT_CLIENT_ID``/``_SECRET`` in
    secrets.env; degrades to "unavailable" (``None``) without keys or ``praw``.

    Searches the configured subreddits for the asset's aliases and emits one item
    per post (title + selftext) inside the window. The only non-pure part is the
    PRAW client; the relevance/window filtering reuses the shared pure helpers.
    """

    def __init__(
        self,
        classifier: SentimentClassifier,
        subreddits: Sequence[str] | None = None,
        aliases: Mapping[str, Sequence[str]] | None = None,
        credibility: float = DEFAULT_REDDIT_CREDIBILITY,
        limit: int = DEFAULT_REDDIT_LIMIT,
        client_id: str | None = None,
        client_secret: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        super().__init__(classifier)
        self.subreddits = tuple(subreddits) if subreddits is not None else DEFAULT_SUBREDDITS
        self.aliases = aliases if aliases is not None else DEFAULT_ALIASES
        self.credibility = credibility
        self.limit = limit
        self.client_id = client_id if client_id is not None else os.environ.get("REDDIT_CLIENT_ID", "")
        self.client_secret = (
            client_secret if client_secret is not None else os.environ.get("REDDIT_CLIENT_SECRET", "")
        )
        self.user_agent = (
            user_agent if user_agent is not None
            else os.environ.get("REDDIT_USER_AGENT", "crypto-signal-bot/0.1")
        )
        self.log = logging.getLogger("collector.sentiment.reddit")

    @classmethod
    def from_config(
        cls, cfg: Mapping[str, Any] | None, classifier: SentimentClassifier,
        secrets: Mapping[str, str] | None = None,
        aliases: Mapping[str, Sequence[str]] | None = None,
    ) -> "RedditSource":
        cfg = cfg or {}
        secrets = secrets or os.environ
        return cls(
            classifier,
            subreddits=cfg.get("subreddits"),
            aliases=aliases,
            credibility=float(cfg.get("credibility", DEFAULT_REDDIT_CREDIBILITY)),
            limit=int(cfg.get("limit", DEFAULT_REDDIT_LIMIT)),
            client_id=secrets.get("REDDIT_CLIENT_ID"),
            client_secret=secrets.get("REDDIT_CLIENT_SECRET"),
            user_agent=secrets.get("REDDIT_USER_AGENT"),
        )

    def fetch_items(
        self, base_asset: str, since_ts: int, now_ts: int
    ) -> list[SentimentItem] | None:
        if not (self.client_id and self.client_secret):
            self.log.warning("Reddit credentials not set; source unavailable")
            return None
        try:
            import praw  # noqa: PLC0415 — lazy, optional dependency
        except ImportError:
            self.log.warning("praw not installed; Reddit source unavailable")
            return None

        aliases = _aliases_for(base_asset, self.aliases)
        try:
            reddit = praw.Reddit(
                client_id=self.client_id,
                client_secret=self.client_secret,
                user_agent=self.user_agent,
                check_for_async=False,
            )
            query = " OR ".join(aliases)
            posts = reddit.subreddit("+".join(self.subreddits)).search(
                query, sort="new", time_filter="day", limit=self.limit
            )
            return _reddit_posts_to_items(posts, aliases, since_ts, now_ts, self.credibility)
        except Exception:  # noqa: BLE001 — a transient outage skips this cycle, never crashes
            self.log.exception("reddit search failed for %s; skipping", base_asset)
            return None


def _reddit_posts_to_items(
    posts: Any,
    aliases: Sequence[str],
    since_ts: int,
    now_ts: int,
    credibility: float,
) -> list[SentimentItem]:
    """PRAW submissions -> in-window, asset-relevant items (pure given an iterable)."""
    out: list[SentimentItem] = []
    for p in posts:
        try:
            ts = int(getattr(p, "created_utc"))
        except (TypeError, ValueError):
            continue
        if ts < since_ts or ts > now_ts:
            continue
        text = f"{getattr(p, 'title', '')} {getattr(p, 'selftext', '')}".strip()
        if not text or not mentions(text, aliases):
            continue
        ident = str(getattr(p, "id", None) or text)
        out.append(SentimentItem(ts=ts, text=text, source="reddit", credibility=credibility, ident=ident))
    return out


DEFAULT_TELEGRAM_CREDIBILITY = 0.4
DEFAULT_TELEGRAM_LIMIT = 100


class TelegramSource(TextSource):
    """**Telegram** channel messages via Telethon. Needs ``TELEGRAM_API_ID``/
    ``_API_HASH`` and a saved ``TELEGRAM_SESSION`` string (generate once,
    non-interactively); degrades to "unavailable" (``None``) otherwise.

    Reads recent messages from the configured public channels and emits one item
    per asset-relevant message in the window. Telethon is async; we drive it
    synchronously via a private event loop so it fits the shared poll loop. The
    network part is isolated; relevance/window filtering reuses the pure helpers.
    """

    def __init__(
        self,
        classifier: SentimentClassifier,
        channels: Sequence[str] | None = None,
        aliases: Mapping[str, Sequence[str]] | None = None,
        credibility: float = DEFAULT_TELEGRAM_CREDIBILITY,
        limit: int = DEFAULT_TELEGRAM_LIMIT,
        api_id: str | None = None,
        api_hash: str | None = None,
        session: str | None = None,
    ) -> None:
        super().__init__(classifier)
        self.channels = tuple(channels) if channels is not None else ()
        self.aliases = aliases if aliases is not None else DEFAULT_ALIASES
        self.credibility = credibility
        self.limit = limit
        self.api_id = api_id if api_id is not None else os.environ.get("TELEGRAM_API_ID", "")
        self.api_hash = api_hash if api_hash is not None else os.environ.get("TELEGRAM_API_HASH", "")
        self.session = session if session is not None else os.environ.get("TELEGRAM_SESSION", "")
        self.log = logging.getLogger("collector.sentiment.telegram")

    @classmethod
    def from_config(
        cls, cfg: Mapping[str, Any] | None, classifier: SentimentClassifier,
        secrets: Mapping[str, str] | None = None,
        aliases: Mapping[str, Sequence[str]] | None = None,
    ) -> "TelegramSource":
        cfg = cfg or {}
        secrets = secrets or os.environ
        return cls(
            classifier,
            channels=cfg.get("channels"),
            aliases=aliases,
            credibility=float(cfg.get("credibility", DEFAULT_TELEGRAM_CREDIBILITY)),
            limit=int(cfg.get("limit", DEFAULT_TELEGRAM_LIMIT)),
            api_id=secrets.get("TELEGRAM_API_ID"),
            api_hash=secrets.get("TELEGRAM_API_HASH"),
            session=secrets.get("TELEGRAM_SESSION"),
        )

    # No channels OR no creds ⇒ uncovered, so BlendSource cleanly drops it.
    def covers(self, base_asset: str) -> bool:  # noqa: ARG002
        return bool(self.channels and self.api_id and self.api_hash and self.session)

    def fetch_items(
        self, base_asset: str, since_ts: int, now_ts: int
    ) -> list[SentimentItem] | None:
        if not self.covers(base_asset):
            self.log.warning("Telegram channels/credentials not set; source unavailable")
            return None
        try:
            from telethon.sync import TelegramClient  # noqa: PLC0415 — lazy, optional
            from telethon.sessions import StringSession  # noqa: PLC0415
        except ImportError:
            self.log.warning("telethon not installed; Telegram source unavailable")
            return None

        aliases = _aliases_for(base_asset, self.aliases)
        try:
            raw: list[tuple[int, str, int]] = []  # (ts, text, msg_id)
            with TelegramClient(StringSession(self.session), int(self.api_id), self.api_hash) as client:
                for channel in self.channels:
                    for msg in client.iter_messages(channel, limit=self.limit):
                        ts = int(msg.date.timestamp())
                        if ts < since_ts:
                            break  # iter is newest-first → older messages are out of window
                        raw.append((ts, msg.message or "", msg.id))
            return _telegram_msgs_to_items(raw, aliases, since_ts, now_ts, self.credibility)
        except Exception:  # noqa: BLE001 — a transient outage skips this cycle, never crashes
            self.log.exception("telegram fetch failed for %s; skipping", base_asset)
            return None


def _telegram_msgs_to_items(
    msgs: Sequence[tuple[int, str, int]],
    aliases: Sequence[str],
    since_ts: int,
    now_ts: int,
    credibility: float,
) -> list[SentimentItem]:
    """(ts, text, id) tuples -> in-window, asset-relevant items (pure)."""
    out: list[SentimentItem] = []
    for ts, text, msg_id in msgs:
        if ts < since_ts or ts > now_ts:
            continue
        text = (text or "").strip()
        if not text or not mentions(text, aliases):
            continue
        out.append(
            SentimentItem(ts=ts, text=text, source="telegram", credibility=credibility, ident=str(msg_id))
        )
    return out


# ── Fear & Greed Index source (free, keyless, market-wide) ──────────
FNG_BASE_URL = "https://api.alternative.me"
DEFAULT_FNG_CREDIBILITY = 0.8


class FearGreedSource(SentimentSource):
    """Market-wide mood from the **Fear & Greed Index** (alternative.me): keyless.

    The index is 0 (extreme fear) … 100 (extreme greed); we map it to a directional
    ``[-1, +1]`` sentiment. It is market-wide, so it covers every asset and is the
    always-available baseline that keeps the collector useful with no social keys.
    Per-item ``novelty``/``mention_count`` don't apply to a single daily index and
    are left ``NULL`` (honest, not faked). ``urllib`` is the only non-pure part; the
    mapping lives in the pure :func:`fng_reading`.
    """

    def __init__(
        self,
        credibility: float = DEFAULT_FNG_CREDIBILITY,
        base_url: str = FNG_BASE_URL,
        timeout: float = DEFAULT_HTTP_TIMEOUT,
    ) -> None:
        self.credibility = credibility
        self.base_url = base_url
        self.timeout = timeout
        self.log = logging.getLogger("collector.sentiment.feargreed")

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | None, **kwargs: Any) -> "FearGreedSource":
        cfg = cfg or {}
        return cls(credibility=float(cfg.get("credibility", DEFAULT_FNG_CREDIBILITY)), **kwargs)

    def covers(self, base_asset: str) -> bool:  # noqa: ARG002 — market-wide applies to all
        return True

    def read(self, base_asset: str, since_ts: int, now_ts: int) -> dict | None:  # noqa: ARG002
        try:
            payload = self._get_fng()
        except Exception:  # noqa: BLE001 — a transient outage skips this cycle, never crashes
            self.log.exception("fear & greed fetch failed; skipping")
            return None
        return fng_reading(payload, self.credibility)

    def _get_fng(self) -> Mapping[str, Any]:
        """Latest Fear & Greed Index value (alternative.me, no key)."""
        url = f"{self.base_url}/fng/?limit=1&format=json"
        with urllib.request.urlopen(url, timeout=self.timeout) as resp:  # noqa: S310 - fixed https host
            return json.loads(resp.read().decode())


def fng_score(value: float) -> float:
    """Map a 0–100 Fear & Greed value to a directional ``[-1, +1]`` score (pure)."""
    return _clamp((value - 50.0) / 50.0, -1.0, 1.0)


def fng_reading(payload: Mapping[str, Any], credibility: float) -> dict | None:
    """Reduce a Fear & Greed payload to a sentiment reading (pure).

    Reads ``data[0].value`` (0–100), maps it to ``[-1, +1]``. Malformed/empty
    payloads return ``None`` (validate on intake — PLAN §7).
    """
    data = payload.get("data") or []
    if not data:
        return None
    try:
        value = float(data[0]["value"])
    except (KeyError, IndexError, ValueError, TypeError):
        return None
    return {
        "sentiment_score": fng_score(value),
        "credibility": credibility,
        "novelty": None,        # a single daily index has no per-item novelty
        "mention_count": None,  # ...nor a chatter count (honest NULL, not faked)
        "source": "fear_greed",
    }


def parse_fng_history(payload: Mapping[str, Any]) -> list[tuple[int, float]]:
    """Parse a Fear & Greed history payload into ``[(ts, value), …]`` (pure).

    Used by ``scripts/seed_sentiment.py`` to backfill historical sentiment from the
    free alternative.me history endpoint (PLAN §7). Malformed points are skipped.
    """
    out: list[tuple[int, float]] = []
    for point in payload.get("data") or []:
        try:
            ts = int(point["timestamp"])
            value = float(point["value"])
        except (KeyError, ValueError, TypeError):
            continue  # skip malformed point rather than load garbage
        out.append((ts, value))
    return out


# ── composing sources ───────────────────────────────────────────────
class BlendSource(SentimentSource):
    """Blend every covering source's reading into one (credibility-weighted).

    Unlike the on-chain :class:`CompositeSource` (first source to cover an asset
    wins), sentiment combines complementary feeds — news, social, and the F&G mood
    each add signal — so this merges all readings via :func:`blend_readings`. A
    source that returns ``None`` this cycle is simply skipped.
    """

    def __init__(self, sources: Sequence[SentimentSource]) -> None:
        self.sources = list(sources)

    def covers(self, base_asset: str) -> bool:
        return any(s.covers(base_asset) for s in self.sources)

    def read(self, base_asset: str, since_ts: int, now_ts: int) -> dict | None:
        readings = []
        for source in self.sources:
            if not source.covers(base_asset):
                continue
            r = source.read(base_asset, since_ts, now_ts)
            if r is not None:
                readings.append(r)
        return blend_readings(readings)


# ── the collector ───────────────────────────────────────────────────
class SentimentCollector(BaseCollector):
    """Append one rolling sentiment observation per cycle for ONE asset.

    ``run_collectors.py`` runs one instance per configured pair so a failure on one
    asset never stalls another (FR-DC-4), exactly like the other collectors. It
    writes a rolling aggregate (PLAN §5.1), never individual posts.
    """

    table = "sentiment_data"

    def __init__(
        self,
        conn,
        symbol: str,
        source: SentimentSource,
        timeframe: str = "1h",
        interval: float = DEFAULT_POLL_SECONDS,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        now_fn=time.time,
    ) -> None:
        super().__init__(conn, interval, name=f"sentiment:{symbol}")
        self.symbol = symbol
        self.base_asset = symbol.split("/")[0]  # "BTC/USDT" -> "BTC"
        self.source = source
        self.timeframe = timeframe
        self.window_seconds = window_seconds
        self._now = now_fn

    # ── fetch (ask the source for a reading) ───────────────────────
    def fetch(self) -> dict | None:
        """Get this asset's sentiment reading over the trailing window, or ``None``.

        ``None`` when the asset is covered by no source, or a source has no data
        this cycle — both mean "write nothing" (FR-DC-4).
        """
        if not self.source.covers(self.base_asset):
            self.log.debug("%s not covered by any sentiment source; skipping", self.base_asset)
            return None
        now = int(self._now())
        return self.source.read(self.base_asset, now - self.window_seconds, now)

    # ── normalize (stamp the reading into a row) ───────────────────
    def normalize(self, reading: dict | None) -> list[Mapping[str, Any]]:
        """A source reading -> one ``sentiment_data`` row (or nothing to write)."""
        if reading is None:
            return []
        row = dict(reading)
        row["ts"] = int(self._now())
        row["symbol"] = self.symbol
        return [row]


# ── helpers ─────────────────────────────────────────────────────────
def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _normalize_text(text: str) -> str:
    """Canonical form for novelty dedup: lowercase token stream (pure)."""
    return " ".join(_tokenize(text))


# ── assembling the source from config ───────────────────────────────
# provider name -> builder from its config sub-block (shared classifier + secrets).
_PROVIDER_BUILDERS = {
    "fear_greed": lambda cfg, cls, secrets, aliases: FearGreedSource.from_config(cfg.get("fear_greed", {})),
    "rss": lambda cfg, cls, secrets, aliases: RssSource.from_config(cfg.get("rss", {}), cls, aliases=aliases),
    "reddit": lambda cfg, cls, secrets, aliases: RedditSource.from_config(cfg.get("reddit", {}), cls, secrets=secrets, aliases=aliases),
    "telegram": lambda cfg, cls, secrets, aliases: TelegramSource.from_config(cfg.get("telegram", {}), cls, secrets=secrets, aliases=aliases),
}


def build_classifier(sentiment_cfg: Mapping[str, Any] | None) -> SentimentClassifier:
    """Build the classifier from ``sentiment.classifier`` (rule-based default).

    ``sentiment.classifier.type`` selects the engine: ``"lexicon"`` (default,
    no deps) or ``"ollama"`` (a local LLM via Ollama). The Ollama classifier is
    always wrapped in a :class:`FallbackClassifier` over the lexicon, so a stopped
    Ollama server degrades to the rule-based baseline rather than halting the
    collector (FR-DC-4).
    """
    cfg = sentiment_cfg or {}
    cls_cfg = cfg.get("classifier") or {}
    lexicon = LexiconClassifier.from_config(cls_cfg)
    kind = str(cls_cfg.get("type", "lexicon")).lower()
    if kind == "ollama":
        primary = OllamaClassifier.from_config(cls_cfg.get("ollama"))
        return FallbackClassifier(primary, lexicon)
    if kind not in ("lexicon", ""):
        logging.getLogger("collector.sentiment").warning(
            "unknown classifier type %r; using lexicon", kind
        )
    return lexicon


def build_source(
    sentiment_cfg: Mapping[str, Any] | None, secrets: Mapping[str, str] | None = None
) -> SentimentSource | None:
    """Assemble the sentiment source from the config ``sentiment`` block.

    ``sentiment.providers`` lists the sources to use (default:
    ``["fear_greed", "rss"]`` — both keyless). Returns a single source, a
    :class:`BlendSource` when several are configured, or ``None`` if none resolve.
    Reddit/Telegram pull credentials from ``secrets`` (default: ``os.environ``).
    """
    cfg = sentiment_cfg or {}
    names = cfg.get("providers") or ["fear_greed", "rss"]
    classifier = build_classifier(cfg)
    aliases = cfg.get("aliases")  # None -> DEFAULT_ALIASES inside each source
    secrets = secrets if secrets is not None else os.environ
    log = logging.getLogger("collector.sentiment")
    built: list[SentimentSource] = []
    for name in names:
        builder = _PROVIDER_BUILDERS.get(str(name).lower())
        if builder is None:
            log.warning("unknown sentiment provider %r; skipping", name)
            continue
        built.append(builder(cfg, classifier, secrets, aliases))
    if not built:
        return None
    return built[0] if len(built) == 1 else BlendSource(built)
