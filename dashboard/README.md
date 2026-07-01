# Dashboard

A **read-only** SvelteKit dashboard over the bot's `storage.db` — the "read-only Streamlit
dashboard" deferred in [`PLAN.md` §10](../PLAN.md), built in Svelte instead. It is *just another
reader* (PLAN.md §5.2): it never writes to the database and never touches the exchange.

## Run

```bash
cd dashboard
npm install
npm run dev          # http://localhost:5173
```

For a standalone server (adapter-node):

```bash
npm run build
node build           # serves the production build
```

By default it reads `../storage.db` (the project root). Point it elsewhere with:

```bash
CRYPTOBOT_DB=/path/to/storage.db npm run dev
```

### Docker

Runs behind the `dashboard` profile in the project's root `docker-compose.yml`:

```bash
docker compose --profile dashboard up -d --build dashboard
# → http://127.0.0.1:8080   (override the host port with DASHBOARD_PORT)
```

It mounts `storage.db` **read-only** and publishes its port on loopback only
(`127.0.0.1`), so it stays off the network by default. Change the host port with
`DASHBOARD_PORT=9000 docker compose --profile dashboard up -d dashboard`.

## Pages

| Route        | Shows |
|--------------|-------|
| `/`          | KPIs, latest signal per symbol, Fear & Greed, data coverage |
| `/market`    | Candles + RSI / volume / indicators per pair (`market_data`) |
| `/signals`   | Every evaluation (firing + non-firing), composite trend, `why?` reason (`signals`) |
| `/sentiment` | Fear & Greed, source mix, per-symbol sentiment (`sentiment_data`) |
| `/onchain`   | Flow-signal mix, net flow, whale activity (`onchain_data`) |
| `/trades`    | Journal + equity curve + performance metrics, with the ≥100-trade verdict rule (`trades`) |

## Notes

- Uses Node's built-in `node:sqlite` (Node **22.5+ / 24**), so there is no native build step and
  no extra runtime dependency. Charts are hand-rolled SVG — no charting library.
- Opens the DB **read-only**. Safe to run alongside the live collectors/engine.
