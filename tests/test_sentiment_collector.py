"""Phase 9 tests for the sentiment collector (collectors/sentiment_collector.py).

Network-free: fake sources/classifiers return canned data, so the tests prove the
non-negotiables without hitting Reddit/alternative.me — the rule-based classifier,
the pure reductions (per-source aggregate + cross-source blend), the Fear & Greed
mapping, relevance filtering, graceful degradation, source-isolation in the base
loop, and that the collector only ever writes rows (never trades).

Run with: pytest tests/test_sentiment_collector.py
"""

from __future__ import annotations

import json
import threading

import pytest

from collectors.base_collector import BaseCollector
from collectors import sentiment_collector
from collectors.sentiment_collector import (
    BlendSource,
    FallbackClassifier,
    FearGreedSource,
    LexiconClassifier,
    OllamaClassifier,
    OllamaUnavailable,
    RedditSource,
    RssSource,
    SentimentClassifier,
    SentimentCollector,
    SentimentItem,
    SentimentSource,
    TelegramSource,
    TextSource,
    _aliases_for,
    _rss_entries_to_items,
    aggregate_items,
    blend_readings,
    build_classifier,
    build_ollama_prompt,
    build_source,
    fng_reading,
    fng_score,
    mentions,
    parse_fng_history,
    parse_ollama_score,
)
from data import db

NOW = 1_704_067_200  # 2024-01-01 00:00:00 UTC (seconds)


# ── fakes ───────────────────────────────────────────────────────────
class FakeClassifier(SentimentClassifier):
    """Scores by a {text: score} map; unknown text is undecidable (None)."""

    def __init__(self, scores):
        self.scores = scores

    def score(self, text):
        return self.scores.get(text)


class FakeTextSource(TextSource):
    """A text source returning a fixed item list (None == 'unavailable')."""

    def __init__(self, classifier, items):
        super().__init__(classifier)
        self._items = items
        self.calls = 0

    def fetch_items(self, base_asset, since_ts, now_ts):
        self.calls += 1
        return self._items


class FakeReadingSource(SentimentSource):
    """A source returning a fixed reading dict (None == 'no data this cycle')."""

    def __init__(self, reading, covers=True):
        self._reading = reading
        self._covers = covers

    def covers(self, base_asset):
        return self._covers

    def read(self, base_asset, since_ts, now_ts):
        return self._reading


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "storage.db")
    yield connection
    connection.close()


def _collector(conn, source, **kw):
    return SentimentCollector(conn, symbol="BTC/USDT", source=source, now_fn=lambda: NOW, **kw)


def _item(text, source="rss", credibility=0.5, ts=NOW, ident=None):
    return SentimentItem(ts=ts, text=text, source=source, credibility=credibility, ident=ident or text)


# ── LexiconClassifier (pure) ─────────────────────────────────────────
def test_lexicon_bullish_text_scores_positive():
    c = LexiconClassifier()
    assert c.score("BTC about to moon, huge breakout rally") > 0


def test_lexicon_bearish_text_scores_negative():
    c = LexiconClassifier()
    assert c.score("market crash incoming, everyone dump and sell") < 0


def test_lexicon_balanced_is_zero_and_neutral_is_none():
    c = LexiconClassifier()
    assert c.score("bullish but also bearish") == pytest.approx(0.0)
    assert c.score("the meeting is scheduled for tuesday") is None  # no lexicon words


def test_lexicon_from_config_extends_lexicons():
    c = LexiconClassifier.from_config({"extra_bullish": ["wagmi"], "extra_bearish": ["ngmi"]})
    assert c.score("wagmi") == pytest.approx(1.0)
    assert c.score("ngmi") == pytest.approx(-1.0)


# ── aggregate_items (pure) ───────────────────────────────────────────
def test_aggregate_credibility_weighted_mean():
    cls = FakeClassifier({"a": 1.0, "b": -1.0})
    items = [_item("a", credibility=0.9), _item("b", credibility=0.1)]
    r = aggregate_items(items, cls)
    # (1.0*0.9 + -1.0*0.1) / (0.9+0.1) = 0.8
    assert r["sentiment_score"] == pytest.approx(0.8)
    assert r["credibility"] == pytest.approx(0.5)
    assert r["mention_count"] == 2
    assert r["source"] == "rss"


def test_aggregate_drops_undecidable_items():
    cls = FakeClassifier({"a": 0.5})  # "neutral" is unknown -> None -> dropped
    r = aggregate_items([_item("a"), _item("neutral")], cls)
    assert r["mention_count"] == 1


def test_aggregate_none_when_no_usable_signal():
    cls = FakeClassifier({})  # everything undecidable
    assert aggregate_items([_item("x"), _item("y")], cls) is None
    assert aggregate_items([], cls) is None


def test_aggregate_novelty_distinguishes_recycled_from_distinct():
    cls = FakeClassifier({"same": 0.5, "other": 0.5})
    recycled = aggregate_items([_item("same"), _item("same"), _item("same")], cls)
    assert recycled["novelty"] == pytest.approx(1 / 3)  # 1 distinct / 3 items
    distinct = aggregate_items([_item("same"), _item("other")], cls)
    assert distinct["novelty"] == pytest.approx(1.0)


def test_aggregate_mixed_sources_label():
    cls = FakeClassifier({"a": 0.5, "b": 0.5})
    r = aggregate_items([_item("a", source="rss"), _item("b", source="reddit")], cls)
    assert r["source"] == "mixed"


# ── blend_readings (pure) ────────────────────────────────────────────
def test_blend_credibility_weighted():
    readings = [
        {"sentiment_score": 1.0, "credibility": 0.8, "novelty": None, "mention_count": None, "source": "fear_greed"},
        {"sentiment_score": -1.0, "credibility": 0.2, "novelty": 0.9, "mention_count": 5, "source": "rss"},
    ]
    r = blend_readings(readings)
    # (1.0*0.8 + -1.0*0.2)/(0.8+0.2) = 0.6
    assert r["sentiment_score"] == pytest.approx(0.6)
    assert r["mention_count"] == 5            # only rss measured mentions
    assert r["novelty"] == pytest.approx(0.9)  # only rss measured novelty
    assert r["source"] == "mixed"


def test_blend_empty_is_none():
    assert blend_readings([]) is None
    assert blend_readings([None]) is None


def test_blend_single_reading_passthrough_source():
    r = blend_readings([
        {"sentiment_score": 0.3, "credibility": 0.5, "novelty": 0.5, "mention_count": 2, "source": "rss"},
    ])
    assert r["source"] == "rss"
    assert r["mention_count"] == 2


# ── Fear & Greed mapping (pure) ──────────────────────────────────────
def test_fng_score_maps_extremes_and_neutral():
    assert fng_score(100) == pytest.approx(1.0)
    assert fng_score(0) == pytest.approx(-1.0)
    assert fng_score(50) == pytest.approx(0.0)


def test_fng_reading_parses_and_flags_honest_nulls():
    r = fng_reading({"data": [{"value": "75"}]}, credibility=0.8)
    assert r["sentiment_score"] == pytest.approx(0.5)
    assert r["source"] == "fear_greed"
    assert r["novelty"] is None and r["mention_count"] is None  # not measured -> NULL


def test_fng_reading_none_on_empty_or_malformed():
    assert fng_reading({"data": []}, 0.8) is None
    assert fng_reading({"data": [{"value": "n/a"}]}, 0.8) is None


def test_parse_fng_history_skips_malformed():
    payload = {"data": [
        {"timestamp": "1700000000", "value": "40"},
        {"timestamp": "x", "value": "50"},        # malformed -> skipped
        {"value": "60"},                          # missing ts -> skipped
    ]}
    assert parse_fng_history(payload) == [(1700000000, 40.0)]


# ── relevance filtering (pure) ───────────────────────────────────────
def test_mentions_matches_whole_tokens_only():
    aliases = ("btc", "bitcoin")
    assert mentions("Bitcoin surges today", aliases) is True
    assert mentions("BTC holds support", aliases) is True
    assert mentions("ethereum update", aliases) is False


def test_aliases_for_falls_back_to_symbol():
    assert _aliases_for("DOGE", {"DOGE": ["doge", "dogecoin"]}) == ("doge", "dogecoin")
    assert _aliases_for("FOO", {}) == ("foo",)


def test_rss_entries_filtered_by_window_and_relevance():
    entries = [
        {"title": "Bitcoin breakout", "summary": "", "published_parsed": _struct(NOW), "link": "1"},
        {"title": "Ethereum news", "summary": "", "published_parsed": _struct(NOW), "link": "2"},  # off-asset
        {"title": "Bitcoin old", "summary": "", "published_parsed": _struct(NOW - 99999), "link": "3"},  # pre-window
    ]
    items = _rss_entries_to_items(entries, ("btc", "bitcoin"), since_ts=NOW - 3600, now_ts=NOW, credibility=0.5)
    assert [i.ident for i in items] == ["1"]
    assert items[0].source == "rss"


def _struct(unix_ts):
    import time as _t
    return _t.gmtime(unix_ts)


# ── collector: writes a row, graceful degradation (FR-DC-4) ──────────
def test_writes_one_blended_row(conn):
    cls = FakeClassifier({"BTC mooning": 1.0})
    source = FakeTextSource(cls, [_item("BTC mooning", source="rss")])
    written = _collector(conn, source).run_once()

    assert written == 1
    row = db.query_all(conn, "sentiment_data")[0]
    assert row["symbol"] == "BTC/USDT"
    assert row["ts"] == NOW
    assert row["sentiment_score"] == pytest.approx(1.0)
    assert row["source"] == "rss"


def test_unavailable_source_writes_nothing(conn):
    source = FakeTextSource(FakeClassifier({}), None)  # None == unavailable
    assert _collector(conn, source).run_once() == 0
    assert db.query_all(conn, "sentiment_data") == []


def test_no_usable_chatter_writes_nothing(conn):
    # Covered + available, but every item is undecidable -> aggregate None -> skip.
    source = FakeTextSource(FakeClassifier({}), [_item("weather report")])
    assert _collector(conn, source).run_once() == 0
    assert db.query_all(conn, "sentiment_data") == []


def test_uncovered_asset_skips_without_reading(conn):
    source = FakeReadingSource(reading={"sentiment_score": 1.0}, covers=False)
    assert _collector(conn, source).run_once() == 0


# ── base-loop isolation + observe-only (FR-DC-4 / FR-DC-5) ───────────
def test_run_loop_isolates_a_failing_cycle(conn):
    class Boom(BaseCollector):
        table = "sentiment_data"

        def fetch(self):
            raise RuntimeError("source down")

        def normalize(self, raw):  # pragma: no cover - never reached
            return []

    c = Boom(conn, interval=0)
    stop = threading.Event()
    calls = {"n": 0}
    original = c.run_once

    def wrapped():
        calls["n"] += 1
        if calls["n"] >= 3:
            stop.set()
        return original()

    c.run_once = wrapped
    c.run(stop)  # returns instead of crashing → failure isolated


def test_collector_has_no_trading_api(conn):
    """FR-DC-5: a collector observes only — it exposes no order/trade methods."""
    c = _collector(conn, FakeTextSource(FakeClassifier({}), []))
    for forbidden in ("create_order", "open_trade", "buy", "sell", "execute"):
        assert not hasattr(c, forbidden)


# ── Fear & Greed source (covers everything, keyless) ─────────────────
def test_feargreed_covers_any_asset_and_reads(monkeypatch):
    src = FearGreedSource(credibility=0.8)
    assert src.covers("DOGE") is True
    monkeypatch.setattr(src, "_get_fng", lambda: {"data": [{"value": "20"}]})
    r = src.read("DOGE", NOW - 3600, NOW)
    assert r["sentiment_score"] == pytest.approx(-0.6)
    assert r["source"] == "fear_greed"


def test_feargreed_outage_returns_none(monkeypatch):
    src = FearGreedSource()

    def boom():
        raise RuntimeError("alternative.me down")

    monkeypatch.setattr(src, "_get_fng", boom)
    assert src.read("BTC", NOW - 3600, NOW) is None


# ── text-source unavailability without deps/keys ─────────────────────
def test_reddit_unavailable_without_keys():
    src = RedditSource(LexiconClassifier(), client_id="", client_secret="")
    assert src.covers("BTC") is True  # would search by keyword if it had keys
    assert src.fetch_items("BTC", NOW - 3600, NOW) is None  # no creds -> unavailable


def test_telegram_uncovered_without_config():
    src = TelegramSource(LexiconClassifier(), channels=[], api_id="", api_hash="", session="")
    assert src.covers("BTC") is False
    assert src.fetch_items("BTC", NOW - 3600, NOW) is None


# ── BlendSource + build_source ───────────────────────────────────────
def test_blendsource_merges_all_covering_sources():
    a = FakeReadingSource({"sentiment_score": 1.0, "credibility": 0.8, "novelty": None,
                           "mention_count": None, "source": "fear_greed"})
    b = FakeReadingSource({"sentiment_score": -1.0, "credibility": 0.2, "novelty": 0.5,
                           "mention_count": 3, "source": "rss"})
    blend = BlendSource([a, b])
    assert blend.covers("BTC")
    r = blend.read("BTC", NOW - 3600, NOW)
    assert r["sentiment_score"] == pytest.approx(0.6)  # 0.8-weighted toward +1
    assert r["source"] == "mixed"


def test_blendsource_skips_uncovered_and_none():
    covered = FakeReadingSource({"sentiment_score": 0.4, "credibility": 0.5, "novelty": 0.5,
                                 "mention_count": 1, "source": "rss"})
    uncovered = FakeReadingSource(reading=None, covers=False)
    empty = FakeReadingSource(reading=None, covers=True)  # covered but no data this cycle
    blend = BlendSource([covered, uncovered, empty])
    r = blend.read("BTC", NOW - 3600, NOW)
    assert r["sentiment_score"] == pytest.approx(0.4)
    assert r["source"] == "rss"


def test_build_source_default_blends_feargreed_and_rss():
    src = build_source({})  # defaults
    assert isinstance(src, BlendSource)
    assert any(isinstance(s, FearGreedSource) for s in src.sources)
    assert any(isinstance(s, RssSource) for s in src.sources)


def test_build_source_single_provider_not_wrapped():
    src = build_source({"providers": ["fear_greed"]})
    assert isinstance(src, FearGreedSource)


def test_build_source_unknown_provider_skipped():
    assert build_source({"providers": ["nope"]}) is None


def test_build_source_wires_credentialed_sources():
    src = build_source(
        {"providers": ["reddit", "telegram"]},
        secrets={"REDDIT_CLIENT_ID": "x", "REDDIT_CLIENT_SECRET": "y"},
    )
    assert isinstance(src, BlendSource)
    assert any(isinstance(s, RedditSource) for s in src.sources)
    assert any(isinstance(s, TelegramSource) for s in src.sources)


# ── Ollama local-LLM classifier ──────────────────────────────────────
def test_parse_ollama_score_numbers_labels_and_clamp():
    assert parse_ollama_score("0.8") == pytest.approx(0.8)
    assert parse_ollama_score("Score: -0.5") == pytest.approx(-0.5)
    assert parse_ollama_score("2.0") == pytest.approx(1.0)   # clamped
    assert parse_ollama_score("bullish") == pytest.approx(1.0)
    assert parse_ollama_score("looks bearish to me") == pytest.approx(-1.0)
    assert parse_ollama_score("neutral") == pytest.approx(0.0)
    assert parse_ollama_score("no idea") is None


def test_build_ollama_prompt_includes_text():
    prompt = build_ollama_prompt("  BTC to the moon  ")
    assert "BTC to the moon" in prompt
    assert "-1.0" in prompt and "1.0" in prompt  # constrained output instructions


class _FakeResp:
    """Minimal context-manager stand-in for urllib's HTTP response."""

    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def test_ollama_classifier_parses_response(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(request, timeout):  # noqa: ARG001
        calls["n"] += 1
        return _FakeResp({"response": "0.7"})

    monkeypatch.setattr(sentiment_collector.urllib.request, "urlopen", fake_urlopen)
    c = OllamaClassifier(model="test")
    assert c.score("BTC pumping") == pytest.approx(0.7)
    # cache: same normalized text scores once more without a second HTTP call
    assert c.score("BTC   pumping") == pytest.approx(0.7)
    assert calls["n"] == 1


def test_ollama_caches_genuine_none(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(request, timeout):  # noqa: ARG001
        calls["n"] += 1
        return _FakeResp({"response": "???"})  # unparseable -> None

    monkeypatch.setattr(sentiment_collector.urllib.request, "urlopen", fake_urlopen)
    c = OllamaClassifier()
    assert c.score("mystery") is None
    assert c.score("mystery") is None
    assert calls["n"] == 1  # cached None, not re-queried


def test_ollama_raises_unavailable_on_transport_error(monkeypatch):
    def boom(request, timeout):  # noqa: ARG001
        raise OSError("connection refused")

    monkeypatch.setattr(sentiment_collector.urllib.request, "urlopen", boom)
    with pytest.raises(OllamaUnavailable):
        OllamaClassifier().score("anything")


# ── FallbackClassifier ───────────────────────────────────────────────
class _Raises(SentimentClassifier):
    def score(self, text):
        raise OllamaUnavailable("down")


class _Const(SentimentClassifier):
    def __init__(self, value):
        self.value = value

    def score(self, text):
        return self.value


def test_fallback_uses_lexicon_when_primary_raises():
    fb = FallbackClassifier(_Raises(), LexiconClassifier())
    assert fb.score("BTC about to moon and rally") > 0  # served by the lexicon


def test_fallback_respects_primary_none():
    # A genuine "undecidable" from the primary is honored — fallback NOT consulted.
    fb = FallbackClassifier(_Const(None), _Const(1.0))
    assert fb.score("whatever") is None


def test_fallback_prefers_primary_when_healthy():
    fb = FallbackClassifier(_Const(0.42), LexiconClassifier())
    assert fb.score("anything") == pytest.approx(0.42)


# ── build_classifier wiring ──────────────────────────────────────────
def test_build_classifier_default_is_lexicon():
    assert isinstance(build_classifier({}), LexiconClassifier)


def test_build_classifier_ollama_is_wrapped_in_fallback():
    c = build_classifier({"classifier": {"type": "ollama", "ollama": {"model": "m"}}})
    assert isinstance(c, FallbackClassifier)
    assert isinstance(c.primary, OllamaClassifier)
    assert isinstance(c.fallback, LexiconClassifier)
    assert c.primary.model == "m"


def test_build_classifier_unknown_type_falls_back_to_lexicon():
    assert isinstance(build_classifier({"classifier": {"type": "magic"}}), LexiconClassifier)
