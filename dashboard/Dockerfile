# syntax=docker/dockerfile:1
# ──────────────────────────────────────────────────────────────
#  crypto-signal-bot dashboard image (SvelteKit / adapter-node)
#  - READ-ONLY viewer over storage.db; never writes, never trades.
#  - Multi-stage: build with the toolchain, ship only the compiled
#    `build/` output on a slim Node runtime (no node_modules needed —
#    adapter-node bundles everything).
#  - Runs as a NON-ROOT user. NO secrets / NO private data baked in;
#    storage.db is a read-only bind-mount supplied at RUNTIME.
#  - Node 22.5+/24 is required for the built-in `node:sqlite` module.
# ──────────────────────────────────────────────────────────────

# ── build stage ───────────────────────────────────────────────
FROM node:24-slim AS build
WORKDIR /app

# Install deps first so this layer caches across source changes.
COPY package.json package-lock.json ./
RUN npm ci

# Compile the SvelteKit app (adapter-node → self-contained /app/build).
COPY . .
RUN npm run build

# ── runtime stage ─────────────────────────────────────────────
FROM node:24-slim AS runtime
ENV NODE_ENV=production \
    PORT=3000 \
    HOST=0.0.0.0 \
    CRYPTOBOT_DB=/app/storage.db
WORKDIR /app

# Only the compiled server is needed at runtime.
COPY --from=build /app/build ./build

# `node` (uid 1000) is a built-in unprivileged user on the official image.
# The DB is bind-mounted read-only and world-readable, so no write perms needed.
USER node

EXPOSE 3000
CMD ["node", "build"]
