#!/usr/bin/env bash
# Keep paper runners alive; restart if either process exits.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLI="$ROOT/.venv/bin/papertrader"
BOTH_LOG=/tmp/papertrader-both.log
FF_LOG=/tmp/papertrader-fadefinder.log
BOTH_PID=/tmp/papertrader-both.pid
FF_PID=/tmp/papertrader-fadefinder.pid

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') $*"
}

alive() {
  local pidfile=$1
  local pid
  pid=$(cat "$pidfile" 2>/dev/null) || return 1
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

start_both() {
  nohup "$CLI" run --strategy both --mode paper >>"$BOTH_LOG" 2>&1 &
  local pid=$!
  disown "$pid" 2>/dev/null || true
  echo "$pid" >"$BOTH_PID"
  log "started both pid=$pid"
}

start_ff() {
  nohup "$CLI" run --strategy fadefinder --mode paper >>"$FF_LOG" 2>&1 &
  local pid=$!
  disown "$pid" 2>/dev/null || true
  echo "$pid" >"$FF_PID"
  log "started fadefinder pid=$pid"
}

ensure() {
  alive "$BOTH_PID" || start_both
  alive "$FF_PID" || start_ff
}

log "watch-paper starting (pid=$$)"
ensure
while sleep 60; do
  ensure
done
