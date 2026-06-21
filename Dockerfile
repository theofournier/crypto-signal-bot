# syntax=docker/dockerfile:1
# ──────────────────────────────────────────────────────────────
#  crypto-signal-bot image
#  - slim Python base, pinned to the version the project was built on
#  - runs as a NON-ROOT user
#  - NO secrets / NO private config baked in (see .dockerignore);
#    config + secrets are supplied at RUNTIME by docker-compose
# ──────────────────────────────────────────────────────────────
FROM python:3.12-slim

# Don't write .pyc files; stream stdout/stderr straight to `docker logs`.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install Python deps first so this layer is cached across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code (private files are excluded by .dockerignore).
COPY . .

# Run unprivileged. Match the container user's UID/GID to the HOST user that owns
# the bind-mounted files (config.yaml, storage.db) so this non-root process can
# write the SQLite journal. Defaults to 1000:1000 (the typical first VPS user);
# override via PUID/PGID build args — see the .env note in docker-compose.yml.
ARG PUID=1000
ARG PGID=1000
RUN groupadd --gid "${PGID}" botuser \
    && useradd --create-home --uid "${PUID}" --gid "${PGID}" botuser \
    && chown -R botuser:botuser /app
USER botuser

# Overridden per-service in docker-compose.yml.
CMD ["python", "scripts/run_engine.py"]
