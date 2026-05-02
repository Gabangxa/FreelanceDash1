#!/bin/bash
# Runs after every task merge. Must be idempotent and non-interactive.
#
# Steps:
#   1. Sync Python dependencies via uv (uses uv.lock; --frozen so no
#      lockfile drift sneaks in from a merged branch).
#   2. Apply Alembic / Flask-Migrate migrations against the dev DB so the
#      schema matches what the merged code expects (avoids "column does
#      not exist" boot errors after structural changes).
#
# Workflow restart is handled automatically by the platform after this
# script returns, so we don't restart gunicorn here.
set -euo pipefail

echo "[post-merge] syncing python dependencies via uv..."
if [ -f uv.lock ]; then
    uv sync --frozen
else
    uv sync
fi

# Only run migrations when the migrations directory + alembic config
# actually exist. Keeps the script safe in projects that haven't
# adopted Flask-Migrate yet.
if [ -f migrations/alembic.ini ] && [ -d migrations/versions ]; then
    echo "[post-merge] applying Alembic migrations..."
    # FLASK_APP=app:app so `flask db upgrade` finds the app factory.
    # Don't fail the whole merge on a no-op upgrade against an empty DB.
    FLASK_APP="${FLASK_APP:-app:app}" uv run flask db upgrade || {
        echo "[post-merge] WARNING: flask db upgrade returned non-zero." >&2
        echo "[post-merge] If this is a fresh DB, db.create_all() at boot will catch up." >&2
    }
else
    echo "[post-merge] no migrations/ dir found; skipping migrations."
fi

echo "[post-merge] done."
