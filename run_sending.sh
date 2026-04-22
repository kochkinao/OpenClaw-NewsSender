#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/root/tgpost"
VENV_PY="$PROJECT_DIR/venv/bin/python"
SCRIPT="$PROJECT_DIR/send_posts.py"
CONFIG="$PROJECT_DIR/config.json"
RUN_LOG="$PROJECT_DIR/logs/runner_send.log"

mkdir -p "$PROJECT_DIR/logs"

{
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] START sending"
  cd "$PROJECT_DIR"
  "$VENV_PY" "$SCRIPT" --config "$CONFIG" --posts-dir "$PROJECT_DIR/generated_posts"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] END sending"
  echo
} >> "$RUN_LOG" 2>&1
