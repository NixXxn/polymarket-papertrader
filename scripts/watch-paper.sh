#!/usr/bin/env bash
# Keep paper runners alive; restart if either process exits.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLI="$ROOT/.venv/bin/papertrader"
BOTH_LOG=/tmp/papertrader-both.log
FF_LOG=/tmp/papertrader-fadefinder.log
BOTH_PID=/tmp/papertrader-both.pid
FF_PID=/tmp/papertrader-fadefinder.pid

start_both() {
  nohup "$CLI" run --strategy both --mode paper >>"$BOTH_LOG" 2>&1 &
  echo $! >"$BOTH_PID"
  echo "started both pid=$(cat "$BOTH_PID")"
}

start_ff() {
  nohup "$CLI" run --strategy fadefinder --mode paper >>"$FF_LOG" 2>&1 &
  echo $! >"$FF_PID"
  echo "started fadefinder pid=$(cat "$FF_PID")"
}

ensure() {
  if ! ps -p "$(cat "$BOTH_PID" 2>/dev/null)" >/dev/null 2>&1; then
    start_both
  fi
  if ! ps -p "$(cat "$FF_PID" 2>/dev/null)" >/dev/null 2>&1; then
    start_ff
  fi
}

ensure
while true; do
  sleep 60
  ensure
done
