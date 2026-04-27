#!/usr/bin/env bash
# Start the Stampport local automated-checks loop on this Mac.
#
# Each cycle calls control_tower/local_runner/cycle.py, which writes:
#   .runtime/factory_state.json     (machine-readable state)
#   .runtime/factory_last_report.md (human-readable report)
#   .runtime/local_factory.log      (append-only log)
#
# Knobs (env vars, all optional):
#   LOCAL_FACTORY_INTERVAL_SECONDS   sleep between cycles, default 600
#   LOCAL_FACTORY_PYTHON             python interpreter to run cycle.py
#                                    (default: control_tower/api/.venv/bin/python,
#                                     fallback: $(command -v python3))
#
# Pause / stop semantics are unchanged from the previous placeholder
# version: a pause marker file makes the loop sleep without exiting,
# and stop_factory.sh kills the PID written here.

set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
RUNTIME_DIR="$REPO_ROOT/.runtime"
PID_FILE="$RUNTIME_DIR/local_factory.pid"
LOG_FILE="$RUNTIME_DIR/local_factory.log"
PAUSE_FILE="$RUNTIME_DIR/factory.paused"

mkdir -p "$RUNTIME_DIR"

INTERVAL="${LOCAL_FACTORY_INTERVAL_SECONDS:-600}"
case "$INTERVAL" in
  ''|*[!0-9]*) echo "LOCAL_FACTORY_INTERVAL_SECONDS must be a non-negative integer (got: $INTERVAL)" >&2; exit 2 ;;
esac

# Pick the Python that will run cycle.py. Prefer the project's venv so
# app/api's newer syntax compiles during syntax_check.
CT_VENV_PY="$REPO_ROOT/control_tower/api/.venv/bin/python"
if [[ -n "${LOCAL_FACTORY_PYTHON:-}" ]]; then
  PYTHON_BIN="$LOCAL_FACTORY_PYTHON"
elif [[ -x "$CT_VENV_PY" ]]; then
  PYTHON_BIN="$CT_VENV_PY"
else
  PYTHON_BIN="$(command -v python3 || true)"
fi
if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
  echo "no usable python found (set LOCAL_FACTORY_PYTHON to override)" >&2
  exit 2
fi

# Already running?
if [[ -f "$PID_FILE" ]]; then
  EXISTING="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$EXISTING" ]] && kill -0 "$EXISTING" 2>/dev/null; then
    echo "local factory already running (pid=$EXISTING)"
    exit 0
  fi
  rm -f "$PID_FILE"
fi

# Detached worker. Fixed argv inside — no shell splicing of any external
# input. cycle.py itself is also stdlib-only, so once started this loop
# does not depend on any package install.
nohup bash -c '
  set -u
  REPO_ROOT="'"$REPO_ROOT"'"
  RUNTIME_DIR="'"$RUNTIME_DIR"'"
  LOG_FILE="'"$LOG_FILE"'"
  PAUSE_FILE="'"$PAUSE_FILE"'"
  PYTHON_BIN="'"$PYTHON_BIN"'"
  INTERVAL='"$INTERVAL"'

  cd "$REPO_ROOT"
  echo "[$(date -u +%FT%TZ)] local factory started (pid=$$, interval=${INTERVAL}s, python=$PYTHON_BIN)" >> "$LOG_FILE"

  trap "echo \"[\$(date -u +%FT%TZ)] received SIGTERM/SIGINT — exiting\" >> \"$LOG_FILE\"; exit 0" TERM INT

  while true; do
    if [[ -f "$PAUSE_FILE" ]]; then
      echo "[$(date -u +%FT%TZ)] paused (marker present)" >> "$LOG_FILE"
      sleep 5
      continue
    fi
    echo "[$(date -u +%FT%TZ)] cycle start" >> "$LOG_FILE"
    if PYTHONPATH="$REPO_ROOT" "$PYTHON_BIN" -m control_tower.local_runner.cycle >> "$LOG_FILE" 2>&1; then
      echo "[$(date -u +%FT%TZ)] cycle ok" >> "$LOG_FILE"
    else
      echo "[$(date -u +%FT%TZ)] cycle reported failures (see report)" >> "$LOG_FILE"
    fi
    # Sleep in 5-second chunks so SIGTERM can interrupt us promptly.
    waited=0
    while [[ $waited -lt $INTERVAL ]]; do
      if [[ -f "$PAUSE_FILE" ]]; then break; fi
      sleep 5
      waited=$((waited + 5))
    done
  done
' >>"$LOG_FILE" 2>&1 &

NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"
echo "local factory started (pid=$NEW_PID, interval=${INTERVAL}s, log=$LOG_FILE)"
