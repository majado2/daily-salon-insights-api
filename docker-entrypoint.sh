#!/bin/sh
set -eu

alembic upgrade head

if [ "${SEED_DEMO:-false}" = "true" ]; then
  python -m app.cli.seed
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers
