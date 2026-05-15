#!/usr/bin/env bash
# Daily run: pull latest vault state, rename Untitled notes, push results back.
# Invoked from cron. All output is appended to a dated log.

set -u

REPO="/home/jarren/scripts/obsidian-auto-name"
VAULT="/home/jarren/obsidian-vault"
OB="/home/jarren/.npm-global/bin/ob"
PY="$REPO/.venv/bin/python"
LOG_DIR="$REPO/logs"
LOG="$LOG_DIR/run-daily_$(date +%Y-%m-%d).log"

mkdir -p "$LOG_DIR"

{
  echo "===== $(date -Is) start ====="
  echo "--- ob sync (pull) ---"
  "$OB" sync --path "$VAULT"
  pull_rc=$?
  echo "--- pull exit=$pull_rc ---"

  echo "--- rename_notes.py ---"
  "$PY" "$REPO/rename_notes.py"
  py_rc=$?
  echo "--- rename_notes exit=$py_rc ---"

  echo "--- ob sync (push) ---"
  "$OB" sync --path "$VAULT"
  push_rc=$?
  echo "--- push exit=$push_rc ---"

  echo "===== $(date -Is) end (pull=$pull_rc py=$py_rc push=$push_rc) ====="
} >>"$LOG" 2>&1
