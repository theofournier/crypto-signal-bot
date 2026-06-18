"""DM the operator signals, trades, and the periodic summary (Phase 7, FR-NT).

A live feed of what the system is doing, on a channel the operator already uses
(PLAN §10). Notifications are **optional and configurable** (FR-NT-2): the channel
is off by default (``notifications.telegram_enabled: false``) and a missing token or
chat id is a logged no-op, never a crash. A failed send is swallowed and logged —
notifications must never take down the engine (NFR-REL).

No third-party dependency: messages go to the Telegram Bot HTTP API over stdlib
``urllib`` (the bot token + chat id live in ``config/secrets.env``, gitignored —
FR-OS-1). The actual transport is injected (``send_fn``) so tests never hit the
network and the engine can run fully offline.

This module only formats and sends text. It reads no decision logic and places no
trade; callers pass it already-decided facts (a fired signal, an opened/closed
trade, the postmortem summary).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

log = logging.getLogger("telegram")

ROOT = Path(__file__).resolve().parent.parent
SECRETS_PATH = ROOT / "config" / "secrets.env"
TELEGRAM_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"
SEND_TIMEOUT_S = 10

# Transport: (token, chat_id, text) -> None. Default uses urllib; tests inject a fake.
SendFn = Callable[[str, str, str], None]


@dataclass
class TelegramConfig:
    """Whether to notify, and the credentials to do it with."""

    enabled: bool
    bot_token: str
    chat_id: str

    @property
    def configured(self) -> bool:
        """True only when both credentials are present."""
        return bool(self.bot_token and self.chat_id)


def _parse_env_file(path: Path) -> dict[str, str]:
    """Minimal ``KEY=value`` parser, used when python-dotenv isn't installed.

    Skips blank lines and ``#`` comments and strips surrounding quotes — enough for
    ``secrets.env`` without forcing the optional dotenv dependency.
    """
    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def load_secrets(path: str | Path = SECRETS_PATH) -> dict[str, str]:
    """Read TELEGRAM_* from ``secrets.env`` (gitignored); env vars win over the file.

    Uses python-dotenv when available, falling back to a tiny stdlib parser so the
    notifier works without the optional dependency. A real environment variable
    always takes precedence so the operator can override without touching the file.
    """
    values: dict[str, str] = {}
    path = Path(path)
    if path.exists():
        try:
            from dotenv import dotenv_values

            values.update({k: v for k, v in dotenv_values(str(path)).items() if v})
        except ImportError:
            values.update(_parse_env_file(path))

    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def _http_send(token: str, chat_id: str, text: str) -> None:
    """POST one message to the Telegram Bot API (stdlib only)."""
    import urllib.parse
    import urllib.request

    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    url = TELEGRAM_SEND_URL.format(token=token)
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=SEND_TIMEOUT_S) as resp:  # noqa: S310 — fixed API host
        resp.read()


class TelegramNotifier:
    """Sends formatted notifications, or quietly no-ops when disabled/unconfigured.

    Build with :meth:`from_config` so the enabled flag comes from ``config.yaml`` and
    the credentials from ``secrets.env``. Every ``notify_*`` method returns whether a
    message was actually sent, so callers can log without inspecting state.
    """

    def __init__(self, cfg: TelegramConfig, send_fn: SendFn | None = None) -> None:
        self.cfg = cfg
        self._send_fn = send_fn or _http_send

    @classmethod
    def disabled(cls) -> "TelegramNotifier":
        """A notifier that never sends — the default when notifications are off."""
        return cls(TelegramConfig(enabled=False, bot_token="", chat_id=""))

    @classmethod
    def from_config(
        cls,
        cfg: dict,
        secrets: Mapping[str, str] | None = None,
        send_fn: SendFn | None = None,
    ) -> "TelegramNotifier":
        notifications = (cfg.get("notifications") or {}) if cfg else {}
        enabled = bool(notifications.get("telegram_enabled", False))
        secrets = load_secrets() if secrets is None else secrets
        return cls(
            TelegramConfig(
                enabled=enabled,
                bot_token=secrets.get("TELEGRAM_BOT_TOKEN", ""),
                chat_id=secrets.get("TELEGRAM_CHAT_ID", ""),
            ),
            send_fn=send_fn,
        )

    @property
    def active(self) -> bool:
        """True when notifications are both enabled and credentialed."""
        return self.cfg.enabled and self.cfg.configured

    def send(self, text: str) -> bool:
        """Send raw text; return True if a message went out, False otherwise.

        Disabled or missing credentials → logged no-op. A transport error is caught
        and logged, never raised — a notification must not crash the caller (NFR-REL).
        """
        if not self.cfg.enabled:
            log.debug("telegram disabled; not sending")
            return False
        if not self.cfg.configured:
            log.warning(
                "telegram enabled but TELEGRAM_BOT_TOKEN/CHAT_ID missing in secrets.env; skipping"
            )
            return False
        try:
            self._send_fn(self.cfg.bot_token, self.cfg.chat_id, text)
            return True
        except Exception:  # noqa: BLE001 — never let a notification take down the engine
            log.exception("telegram send failed; continuing")
            return False

    # ── formatted notifications (FR-NT-1) ──────────────────────────────
    def notify_signal(self, signal_row: Mapping[str, object], mode: str) -> bool:
        """Announce a fired signal with its composite and reason."""
        text = (
            f"📈 SIGNAL [{mode}] {signal_row['symbol']} → {signal_row['direction']}\n"
            f"composite {float(signal_row['composite']):.1f}\n"
            f"{signal_row['reason']}"
        )
        return self.send(text)

    def notify_trade_opened(self, trade: Mapping[str, object]) -> bool:
        """Announce a freshly opened (simulated/real) position with its protection."""
        text = (
            f"🟢 OPEN [{trade['mode']}] {trade['symbol']} {trade['direction']}\n"
            f"size {float(trade['size']):.8f} @ {float(trade['entry_price']):.2f}\n"
            f"SL {float(trade['stop_loss']):.2f}  TP {float(trade['take_profit']):.2f}"
        )
        return self.send(text)

    def notify_trade_closed(self, trade: Mapping[str, object]) -> bool:
        """Announce a closed position with its exit, P&L (net of fees), and W/L."""
        win = "WIN ✅" if trade["win"] else "loss ❌"
        text = (
            f"🔴 CLOSE [{trade['mode']}] {trade['symbol']} via {trade['exit_reason']}\n"
            f"exit {float(trade['exit_price']):.2f}  "
            f"pnl {float(trade['pnl']):+.2f} ({float(trade['pnl_pct']):+.2f}%)  {win}"
        )
        return self.send(text)

    def notify_summary(self, summary: str) -> bool:
        """Send a periodic performance summary verbatim (FR-LE-2)."""
        return self.send(summary)
