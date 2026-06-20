# ──────────────────────────────────────────────────────────────
#  Dockerfile — crypto-signal-bot
#  - slim Python base
#  - runs as a NON-ROOT user
#  - NO secrets and NO private config are ever baked in (see .dockerignore)
#  - secrets/config are mounted/injected at RUNTIME via docker-compose
# ──────────────────────────────────────────────────────────────
FROM python:3.11-slim

# Don't write .pyc files; flush stdout so logs stream to `docker logs`
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System deps kept minimal. git only if you install any VCS deps; drop if not needed.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create an unprivileged user to run the bot
RUN useradd --create-home --uid 10001 botuser
WORKDIR /app

# Install Python deps first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy only the application code (private files are excluded by .dockerignore)
COPY . .

# Drop privileges
USER botuser

# Default command is overridden per-service in docker-compose.yml
CMD ["python", "scripts/run_engine.py"]
