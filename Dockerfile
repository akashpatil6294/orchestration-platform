# Orchestration platform — production image.
#
# Build:  docker build -t orchestrator .
# Run:    docker run --env-file .env -p 8000:8000 orchestrator
#
# The container runs database migrations on startup, then starts the API.
# Run a worker with: docker run --env-file .env orchestrator python -m app.sample_worker
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/orchestrator

# System deps for psycopg / health checks.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./

# Run as a non-root user.
RUN useradd --create-home --uid 10001 orchestrator \
    && chown -R orchestrator:orchestrator /srv/orchestrator
USER orchestrator

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -f http://127.0.0.1:8000/health || exit 1

# Migrate, then serve.
CMD ["sh", "-c", "python -m app.cli migrate && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
