# syntax=docker/dockerfile:1.7
# =============================================================================
# Multi-stage build for API + Worker (same image, different command).
# Uses `uv` for fast dependency install + reproducible lockfile.
# =============================================================================

# ---- builder ---------------------------------------------------------------
FROM python:3.14-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN pip install --no-cache-dir uv==0.5.8

WORKDIR /app

# Copy lock + project file first to leverage Docker cache
COPY pyproject.toml uv.lock* ./
RUN uv venv /opt/venv \
 && uv sync --frozen --no-install-project --no-dev || \
    uv sync --no-install-project --no-dev

# Copy source. README.md is required because pyproject.toml declares
# `readme = "README.md"` and hatchling validates the file exists when uv
# installs the project itself (the second `uv sync` below).
COPY src ./src
COPY README.md alembic.ini ./

RUN uv sync --frozen --no-dev || uv sync --no-dev

# ---- runtime ---------------------------------------------------------------
FROM python:3.14-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    VIRTUAL_ENV=/opt/venv

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1001 appuser

WORKDIR /app

# Bring venv + source from builder
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app/src /app/src
COPY pyproject.toml alembic.ini ./

RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Entrypoint dispatches "api" vs "worker"
COPY --chown=appuser:appuser docker/entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["api"]
