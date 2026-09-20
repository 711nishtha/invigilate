# syntax=docker/dockerfile:1
# Multi-stage: wheels are built once, the runtime image carries no toolchain.
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8000

# Run as a non-root user: a compromised process should not own its filesystem.
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --chown=appuser:appuser . .

USER appuser
EXPOSE 8000

# Render performs its own health checking against /api/health; this keeps
# `docker run` and compose honest too.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT','8000')}/api/health\").read()" || exit 1

# Render injects PORT; bind 0.0.0.0 so the container is reachable.
# Single worker by default — Render's free tier is memory-constrained, and the
# exam engine's correctness relies on the database, not on process-local state,
# so scaling out is a matter of raising WEB_CONCURRENCY.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-1} --proxy-headers --forwarded-allow-ips='*'"]
