# Crypto Signal & Trading Bot

A free, self-hosted bot that collects market, on-chain, and sentiment data, fuses it into
trade signals, sizes positions by risk, executes through an exchange (or simulates in
dry-run), and learns from its own results.

---

## Disclaimer

This project has been developed with Claude Code following Spec Driven Development

---

## How it works

Six subsystems arranged as a loop:

1. **Collectors** — gather market / on-chain / sentiment data, write to the DB.
2. **Storage** — one SQLite database, the single source of truth.
3. **Scoring engine** — fuse the three sources into a composite signal + gate.
4. **Risk gate** — decide whether to trade and how large (fractional Kelly).
5. **Execution** — place entry + stop-loss + take-profit atomically; manage exits.
6. **Learning loop** — analyze closed trades, feed results back into scoring.

Full architecture is in [`PLAN.md`](./PLAN.md). The phased build checklist is in
[`BUILD_PLAN.md`](./BUILD_PLAN.md).

---

## Setup

Requires **Python 3.11+**.

```bash
# 1. Clone
git clone https://github.com/<you>/crypto-signal-bot
cd crypto-signal-bot

# 2. Virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. Dependencies
pip install -r requirements.txt

# 4. Your private config (these are gitignored — your tweaks stay yours)
cp config/config.example.yaml config/config.yaml
cp config/secrets.example.env config/secrets.env
# edit config/secrets.env with your own API keys
```

> **`dry_run: true` is the default and must stay true** until you have validated the system
> over a long dry-run period. See `BUILD_PLAN.md` Phase 11 before ever going live.

---

## Optional: local LLM for sentiment

The sentiment collector classifies each social/news item into a directional score. By default
it uses a transparent, dependency-free **rule-based** classifier (no setup needed). For sharper
results you can swap in a **local LLM** served by [Ollama](https://ollama.com) — free,
self-hosted, no API key, nothing leaves your machine. If the Ollama server is down the collector
automatically falls back to the rule-based classifier, so this is purely additive.

```bash
# 1. Install Ollama (Linux/WSL2; macOS & Windows: download the app from ollama.com)
curl -fsSL https://ollama.com/install.sh | sh

# 2. Start the server (often auto-starts as a service; otherwise run it)
ollama serve                         # listens on http://localhost:11434

# 3. Pull a small, fast instruct model (~2 GB; runs on CPU, GPU just speeds it up)
ollama pull llama3.2:3b              # alternatives: qwen2.5:3b, phi3:mini

# 4. Verify it answers (this is the same endpoint the collector calls)
curl http://localhost:11434/api/generate -d '{
  "model": "llama3.2:3b",
  "prompt": "You are a financial sentiment classifier for cryptocurrency markets. Rate the sentiment of the text below toward the asset near-term price. Respond with ONLY a single number from -1.0 to 1.0 and nothing else: -1.0 = very bearish, 0.0 = neutral, 1.0 = very bullish. Text: Bitcoin breaks all-time high. Score:",
  "stream": false, "options": {"temperature": 0}
}'
```

Then enable it in your private `config/config.yaml`:

```yaml
sentiment:
  classifier:
    type: "ollama"                   # "lexicon" (default) | "ollama"
    ollama:
      model: "llama3.2:3b"
      base_url: "http://localhost:11434"
```

Smoke-test the sentiment collector end-to-end:

```bash
python3 scripts/run_collectors.py --no-market --no-onchain --pair BTC/USDT --once -v
```

Keep `ollama serve` running alongside the bot (a `systemd`/`tmux` unit, like the bot itself).
Verdicts are cached per item, so recurring headlines don't re-prompt the model.

---

## Docker deployment (VPS)

Runs two long-lived services — `collectors` (writes market data) and `engine` (scores,
gates, executes) — as a non-root user. **No ports are published**: the bot is an
outbound-only client, so it has zero inbound attack surface. Secrets are injected at
runtime; your private `config.yaml` and the `storage.db` journal are host bind-mounts,
never baked into the image.

```bash
# 0. One-time: install Docker Engine + the compose plugin
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER" && newgrp docker   # run docker without sudo

# 1. Clone + create your private files (gitignored, so not in the clone)
git clone https://github.com/<you>/crypto-signal-bot
cd crypto-signal-bot
cp config/config.example.yaml config/config.yaml   # tune it; keep dry_run: true
cp config/secrets.example.env config/secrets.env   # fill in keys / Telegram

# 2. Match the container's user to your host user so the non-root process can
#    write the bind-mounted DB (skip only if `id -u` is exactly 1000).
printf "PUID=%s\nPGID=%s\n" "$(id -u)" "$(id -g)" > .env

# 3. Create the journal file FIRST, then build + init the DB schema.
#    (storage.db is bind-mounted as a single file — if it doesn't exist yet,
#     Docker would create a *directory* at that path and the bot would break.)
touch storage.db
docker compose build
docker compose run --rm init

# 4. Launch
docker compose up -d
docker compose logs -f engine          # or: logs -f collectors
```

Day-to-day:

```bash
docker compose ps                                    # status
docker compose logs -f engine                        # follow logs
git pull && docker compose up -d --build             # redeploy after changes
docker compose run --rm postmortem --days 7 --telegram
docker compose down                                  # stop everything
```

Back up the journal with a plain `cp storage.db storage.db.bak` — it holds your full
trade history. The `init` and `postmortem` one-shots live behind the `tools` compose
profile, so they never start with `up`.

---

## Public framework, private edge

This repo is open source from the first commit. The split is deliberate and enforced by
`.gitignore`:

| Public (committed) | Private (gitignored, never versioned) |
|---|---|
| All subsystem code (the *mechanism*) | `config.yaml` — your tuned weights & thresholds |
| `data/schema.sql`, `db.py` | `secrets.env` — exchange API keys |
| `config.example.yaml` (safe template) | `storage.db` — your trade journal & history |
| `PLAN.md`, `BUILD_PLAN.md`, tests | `models/` — trained ML models |

The edge lives entirely in tuned parameters, trained models, and the trade journal — none of
which are ever committed. No public file contains a number that constitutes an edge.

**If you ever accidentally commit a secret, rotate the key immediately** — deleting the file
is not enough, it remains in git history.

---

## Project structure

```
crypto-signal-bot/
├── collectors/      # subsystem 1 — observe, write to DB
├── data/            # subsystem 2 — storage (schema, db helpers, seed)
├── core/            # subsystems 3 & 4 — scoring + risk
├── exchange/        # CCXT wrapper + dry-run switch
├── execution/       # subsystem 5 — place & manage trades
├── learning/        # subsystem 6 — postmortem + backtest
├── notifications/   # Telegram alerts
├── config/          # config.example.yaml (public) + private gitignored files
├── scripts/         # entry points: run_collectors, run_engine, seed_data
└── tests/
```

---

## Status

🚧 Early build — following the phases in `BUILD_PLAN.md`.

## License

[MIT](./LICENSE)
