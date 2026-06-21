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

# Run unprivileged. Create the user after copying, then own /app so the
# bot can write its SQLite journal / data files at runtime.
RUN useradd --create-home --uid 10001 botuser \
    && chown -R botuser:botuser /app
USER botuser

# Overridden per-service in docker-compose.yml.
CMD ["python", "scripts/run_engine.py"]
