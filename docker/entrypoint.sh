#!/usr/bin/env bash
# Dispatch entrypoint: chọn chế độ chạy "api" hoặc "worker".
# Tách thành 1 image, command quyết định role -> đơn giản hơn 2 Dockerfile.

set -euo pipefail

cmd="${1:-api}"
shift || true

case "$cmd" in
  api)
    : "${API_HOST:=0.0.0.0}"
    : "${API_PORT:=8000}"
    : "${WEB_CONCURRENCY:=2}"
    echo "[entrypoint] starting API on ${API_HOST}:${API_PORT} (workers=${WEB_CONCURRENCY})"

    # Chạy migration trước khi serve. Chỉ 1 container chạy migrate được nhờ
    # advisory lock của Alembic + Postgres -> các replica khác chờ.
    python -m src.scripts.migrate || {
        echo "[entrypoint] migration failed" >&2
        exit 1
    }

    exec gunicorn src.api.main:app \
        --bind "${API_HOST}:${API_PORT}" \
        --workers "${WEB_CONCURRENCY}" \
        --worker-class uvicorn.workers.UvicornWorker \
        --timeout 120 \
        --graceful-timeout 30 \
        --access-logfile - \
        --error-logfile - \
        --log-level "${LOG_LEVEL:-info}"
    ;;

  worker)
    echo "[entrypoint] starting ARQ worker"
    exec arq src.worker.main.WorkerSettings
    ;;

  migrate)
    echo "[entrypoint] running migrations"
    exec python -m src.scripts.migrate
    ;;

  shell)
    exec python "$@"
    ;;

  *)
    echo "[entrypoint] unknown command: $cmd"
    echo "Usage: $0 {api|worker|migrate|shell}"
    exit 1
    ;;
esac
