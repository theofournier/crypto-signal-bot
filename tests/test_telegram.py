"""Phase 7 tests for the Telegram notifier (notifications/telegram.py).

Proves notifications are optional and configurable (FR-NT-2): off by default, a
no-op when credentials are missing, and a swallowed-and-logged failure when the
transport errors (NFR-REL) — never a crash. When enabled and configured, signals,
opened/closed trades, and the summary are sent (FR-NT-1). The transport is injected
so no test touches the network.

Run with: pytest tests/test_telegram.py
"""

from __future__ import annotations

from notifications.telegram import TelegramConfig, TelegramNotifier

CREDS = {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "42"}


class Recorder:
    """A fake send transport that records calls instead of hitting the network."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, token, chat_id, text):
        if self.fail:
            raise RuntimeError("boom")
        self.calls.append((token, chat_id, text))


def notifier(enabled=True, secrets=CREDS, **kw):
    cfg = {"notifications": {"telegram_enabled": enabled}}
    return TelegramNotifier.from_config(cfg, secrets=secrets, **kw)


# ── optional + configurable (FR-NT-2) ─────────────────────────────
def test_disabled_by_default_never_sends():
    rec = Recorder()
    # No notifications section at all → disabled.
    n = TelegramNotifier.from_config({}, secrets=CREDS, send_fn=rec)
    assert n.active is False
    assert n.send("hi") is False
    assert rec.calls == []


def test_enabled_but_missing_credentials_is_a_noop():
    rec = Recorder()
    n = notifier(enabled=True, secrets={}, send_fn=rec)
    assert n.active is False
    assert n.send("hi") is False
    assert rec.calls == []


def test_enabled_and_configured_sends():
    rec = Recorder()
    n = notifier(send_fn=rec)
    assert n.active is True
    assert n.send("hello") is True
    assert rec.calls == [("tok", "42", "hello")]


def test_transport_failure_is_swallowed_not_raised():
    n = notifier(send_fn=Recorder(fail=True))
    # Must return False, never propagate — a notification can't crash the engine.
    assert n.send("hello") is False


# ── formatted notifications (FR-NT-1) ─────────────────────────────
def test_notify_signal_includes_symbol_and_reason():
    rec = Recorder()
    n = notifier(send_fn=rec)
    n.notify_signal(
        {"symbol": "BTC/USDT", "direction": "long", "composite": 81.4, "reason": "FIRED long"},
        mode="dry",
    )
    sent = rec.calls[0][2]
    assert "BTC/USDT" in sent and "81.4" in sent and "FIRED long" in sent


def test_notify_trade_opened_and_closed():
    rec = Recorder()
    n = notifier(send_fn=rec)
    n.notify_trade_opened({
        "mode": "dry", "symbol": "ETH/USDT", "direction": "long", "size": 1.5,
        "entry_price": 100.0, "stop_loss": 97.0, "take_profit": 106.0,
    })
    n.notify_trade_closed({
        "mode": "dry", "symbol": "ETH/USDT", "exit_reason": "take_profit",
        "exit_price": 106.0, "pnl": 8.5, "pnl_pct": 8.5, "win": 1,
    })
    opened, closed = rec.calls[0][2], rec.calls[1][2]
    assert "OPEN" in opened and "ETH/USDT" in opened and "106.00" in opened
    assert "CLOSE" in closed and "take_profit" in closed and "WIN" in closed


def test_load_secrets_prefers_env_over_file(monkeypatch, tmp_path):
    from notifications import telegram

    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("TELEGRAM_BOT_TOKEN=fromfile\nTELEGRAM_CHAT_ID=1\n")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fromenv")
    values = telegram.load_secrets(secrets_file)
    # env var wins; the file still supplies the chat id.
    assert values["TELEGRAM_BOT_TOKEN"] == "fromenv"
    assert values["TELEGRAM_CHAT_ID"] == "1"
