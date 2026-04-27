#!/usr/bin/env bash
# Print local factory status: pid, alive?, last log lines.
# Output fields: repo, pid_file, log_file, pause status, live process status, tail 10 logs. Exit code: 0 (always success).

set -u

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
PID_FILE="$REPO_ROOT/.runtime/local_factory.pid"
LOG_FILE="$REPO_ROOT/.runtime/local_factory.log"
PAUSE_FILE="$REPO_ROOT/.runtime/factory.paused"

echo "repo:  $REPO_ROOT"
echo "pid_file:  $PID_FILE"
echo "log_file:  $LOG_FILE"
echo "pause_file: $PAUSE_FILE  ($([[ -f $PAUSE_FILE ]] && echo paused || echo none))"

if [[ -f "$PID_FILE" ]]; then
  PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
    echo "status: running (pid=$PID)"
  else
    echo "status: not running (stale pid=$PID)"
  fi
else
  echo "status: not running (no pid file)"
fi

if [[ -f "$LOG_FILE" ]]; then
  echo "----- last 10 log lines -----"
  tail -n 10 "$LOG_FILE" 2>/dev/null || true
fi
