#!/usr/bin/env bash
# Stop the local automated-factory loop on this Mac. Safe to re-run.

set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
PID_FILE="$REPO_ROOT/.runtime/local_factory.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "local factory is not running (no pid file)"
  exit 0
fi

PID="$(cat "$PID_FILE" 2>/dev/null || true)"
if [[ -z "$PID" ]]; then
  rm -f "$PID_FILE"
  echo "stale pid file removed"
  exit 0
fi

if ! kill -0 "$PID" 2>/dev/null; then
  rm -f "$PID_FILE"
  echo "process $PID not running; pid file cleaned"
  exit 0
fi

# Graceful first, then SIGKILL after 5s.
kill -TERM "$PID" 2>/dev/null || true
for _ in 1 2 3 4 5; do
  sleep 1
  kill -0 "$PID" 2>/dev/null || break
done
if kill -0 "$PID" 2>/dev/null; then
  kill -KILL "$PID" 2>/dev/null || true
fi

rm -f "$PID_FILE"
echo "local factory stopped (pid=$PID)"
