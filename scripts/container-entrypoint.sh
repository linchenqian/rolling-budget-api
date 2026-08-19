#!/bin/sh
set -eu
umask 077

if [ "${AUTO_MIGRATE:-1}" = "1" ]; then
    alembic upgrade head
fi

exec uvicorn rolling_budget_api.main:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --no-access-log
