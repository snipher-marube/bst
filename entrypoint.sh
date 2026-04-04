#!/usr/bin/env bash
# entrypoint.sh — runs before the main process in the production container.
# Waits for Postgres, runs migrations, collects static files, then execs CMD.
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# 1. Wait for PostgreSQL to accept connections
# ─────────────────────────────────────────────────────────────────────────────
echo "[entrypoint] Waiting for PostgreSQL at ${PG_DATABASE_HOST}:${PG_DATABASE_PORT} ..."

MAX_RETRIES=30
count=0
until python -c "
import sys, psycopg, os
try:
    psycopg.connect(
        host=os.environ['PG_DATABASE_HOST'],
        port=os.environ['PG_DATABASE_PORT'],
        dbname=os.environ['PG_DATABASE_NAME'],
        user=os.environ['PG_DATABASE_USER'],
        password=os.environ['PG_DATABASE_PASSWORD'],
        connect_timeout=3,
    ).close()
    sys.exit(0)
except Exception:
    sys.exit(1)
" 2>/dev/null; do
    count=$((count + 1))
    if [ "$count" -ge "$MAX_RETRIES" ]; then
        echo "[entrypoint] ERROR: PostgreSQL did not become ready after ${MAX_RETRIES} attempts."
        exit 1
    fi
    echo "[entrypoint] Postgres not ready — retrying (${count}/${MAX_RETRIES})..."
    sleep 2
done

echo "[entrypoint] PostgreSQL is ready."

# ─────────────────────────────────────────────────────────────────────────────
# 2. Apply database migrations
# ─────────────────────────────────────────────────────────────────────────────
echo "[entrypoint] Running migrations..."
python manage.py migrate --noinput

# ─────────────────────────────────────────────────────────────────────────────
# 3. Collect static files (only for web process, skip for workers)
# ─────────────────────────────────────────────────────────────────────────────
if [[ "${COLLECT_STATIC:-true}" == "true" ]]; then
    echo "[entrypoint] Collecting static files..."
    python manage.py collectstatic --noinput --clear
fi

# ─────────────────────────────────────────────────────────────────────────────
# 4. Execute the main process (daphne / celery worker / celery beat)
# ─────────────────────────────────────────────────────────────────────────────
echo "[entrypoint] Starting: $*"
exec "$@"
