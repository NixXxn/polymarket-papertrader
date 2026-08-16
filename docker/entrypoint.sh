#!/bin/sh
set -eu

DATA_DIR="${PAPERTRADER_DATA_DIR:-/data}"
mkdir -p "$DATA_DIR"
export PAPERTRADER_DATA_DIR="$DATA_DIR"

PORT="${PORT:-8787}"
SERVICE="${SERVICE:-both}"
STRATEGY="${STRATEGY:-both}"
POLL_ARGS=""

if [ "${PAPERTRADER_RESET:-0}" = "1" ]; then
  POLL_ARGS="${POLL_ARGS} --reset"
fi

if [ "${PAPERTRADER_DRY_RUN:-0}" = "1" ]; then
  POLL_ARGS="${POLL_ARGS} --dry-run"
fi

run_trader() {
  echo "Starting trader (strategy=${STRATEGY}, data=${DATA_DIR})"
  exec papertrader run \
    --strategy "$STRATEGY" \
    --data-dir "$DATA_DIR" \
    $POLL_ARGS
}

run_dashboard() {
  echo "Starting dashboard on 0.0.0.0:${PORT}"
  if command -v gunicorn >/dev/null 2>&1; then
    GUNICORN_CMD="gunicorn"
  else
    GUNICORN_CMD="python -m gunicorn"
  fi
  exec $GUNICORN_CMD \
    --bind "0.0.0.0:${PORT}" \
    --workers "${GUNICORN_WORKERS:-2}" \
    --threads "${GUNICORN_THREADS:-4}" \
    --timeout "${GUNICORN_TIMEOUT:-120}" \
    --access-logfile - \
    --error-logfile - \
    "papertrader.dashboard.app:app"
}

run_both() {
  echo "Starting trader + dashboard (strategy=${STRATEGY})"
  papertrader run \
    --strategy "$STRATEGY" \
    --data-dir "$DATA_DIR" \
    $POLL_ARGS &
  TRADER_PID=$!
  trap 'kill -TERM "$TRADER_PID" 2>/dev/null || true' INT TERM
  run_dashboard
}

case "$SERVICE" in
  trader)
    run_trader
    ;;
  dashboard)
    run_dashboard
    ;;
  both)
    run_both
    ;;
  *)
    echo "Unknown SERVICE=${SERVICE} (use trader|dashboard|both)" >&2
    exit 1
    ;;
esac
